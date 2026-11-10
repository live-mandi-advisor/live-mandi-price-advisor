"""
Trains one Prophet model per (commodity, mandi) pair — see docs/build-plan.md,
Section 4 and 4.5. Never trains a single combined "state-level" model.

Variety handling (resolved 8 Nov 2026, see docs/build-plan.md Section 3.3.1):
varieties are averaged together per (commodity, market, arrival_date) rather
than kept as separate series or separate models. Known limitation: for a
handful of commodities with real grade/cultivar splits (Onion 1st/2nd Sort,
Coconut Big/Medium/Small, named premium wheat cultivars), this can blend
genuinely different price tiers into one series if the reported grade mix
shifts over time. Accepted tradeoff for project scope — see Chapter 5
limitations.

Pipeline per eligible (commodity, market) pair:
    pull cleaned daily series from price_records (variety-averaged)
        -> require a minimum history length
        -> holdout split (last N days)
        -> fit Prophet on the training portion
        -> fit a naive/moving-average baseline on the same training portion
        -> compare both against the real holdout days (MAE)
        -> refit Prophet on the FULL series (train + holdout) for the saved model
        -> save via joblib to ml/models/{commodity_id}_{market_id}.pkl
        -> upsert one row into model_registry

Pairs are trained in parallel (joblib) since there can be hundreds of them.
"""

import sys
import time
from pathlib import Path

import pandas as pd
from joblib import Parallel, delayed, dump
from prophet import Prophet
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data_pipeline"))
from db import engine, price_records, commodities, markets, model_registry  # noqa: E402


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Minimum number of distinct days of history required before a
# (commodity, market) pair is even attempted. Prophet can technically run
# on less, but weekly seasonality needs several weeks and yearly
# seasonality needs the better part of a year -- below this, a fitted
# model is more likely to be fitting noise than a real signal.
MIN_HISTORY_DAYS = 180

# Number of most-recent days held out to evaluate MAE. Chosen to match the
# project's confirmed 7-day forecast horizon, but a slightly longer window
# (14 days -> two forecast cycles) gives a less noisy MAE estimate than a
# single week would.
# Forecast horizon used by the application.
FORECAST_HORIZON_DAYS = 7

# Number of historical rolling evaluation windows.
ROLLING_WINDOWS = 3

# Minimum actual price observations required in each
# 7-calendar-day evaluation window.
MIN_HOLDOUT_OBSERVATIONS_PER_WINDOW = 2

# Keep evaluation-only mode enabled.
EVALUATION_ONLY = True
# True = evaluate models without retraining/saving full-data models
# or updating model_registry.
EVALUATION_ONLY = True

MODELS_DIR = Path(__file__).resolve().parent / "models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

N_JOBS = -1  # use all available cores; see Parallel() call at the bottom


# ---------------------------------------------------------------------------
# Step 1 -- find every (commodity, market) pair with enough history
# ---------------------------------------------------------------------------

def find_eligible_pairs(conn) -> pd.DataFrame:
    """
    Returns one row per (commodity_id, market_id) pair that has at least
    MIN_HISTORY_DAYS distinct arrival dates in price_records, along with
    readable commodity/market names for logging.
    """
    print("=" * 70)
    print("STEP 1 -- FINDING ELIGIBLE (COMMODITY, MARKET) PAIRS")
    print("=" * 70)

    statement = (
        select(
            price_records.c.commodity_id,
            price_records.c.market_id,
            commodities.c.name.label("commodity_name"),
            markets.c.name.label("market_name"),
            func.count(func.distinct(price_records.c.arrival_date)).label(
                "num_days"
            ),
        )
        .join(commodities, commodities.c.id == price_records.c.commodity_id)
        .join(markets, markets.c.id == price_records.c.market_id)
        .group_by(
            price_records.c.commodity_id,
            price_records.c.market_id,
            commodities.c.name,
            markets.c.name,
        )
        .having(
            func.count(func.distinct(price_records.c.arrival_date))
            >= MIN_HISTORY_DAYS
        )
    )

    rows = conn.execute(statement).mappings().all()
    pairs_df = pd.DataFrame(rows)

    total_combinations_query = select(
        func.count(func.distinct((price_records.c.commodity_id, price_records.c.market_id)))
    )
    # Not all backends support tuple-distinct cleanly; fall back to a
    # simpler total count for the log line if that happens.
    try:
        total_combinations = conn.execute(total_combinations_query).scalar_one()
    except Exception:
        total_combinations = None

    print(f"Minimum history required : {MIN_HISTORY_DAYS} distinct days")
    if total_combinations is not None:
        print(f"Total (commodity, market) combinations in price_records: {total_combinations:,}")
    print(f"Pairs meeting the threshold: {len(pairs_df):,}")
    print()

    return pairs_df


# ---------------------------------------------------------------------------
# Step 2 -- pull and prepare one pair's series
# ---------------------------------------------------------------------------

def load_series(commodity_id: int, market_id: int, conn) -> pd.DataFrame:
    """
    Returns a DataFrame with columns ds (date) and y (modal_price),
    one row per arrival_date, averaged across varieties for that
    (commodity, market, date) -- see module docstring for why.
    """
    statement = (
        select(
            price_records.c.arrival_date,
            func.avg(price_records.c.modal_price).label("modal_price"),
        )
        .where(
            price_records.c.commodity_id == commodity_id,
            price_records.c.market_id == market_id,
        )
        .group_by(price_records.c.arrival_date)
        .order_by(price_records.c.arrival_date)
    )

    rows = conn.execute(statement).all()
    df = pd.DataFrame(rows, columns=["ds", "y"])
    df["ds"] = pd.to_datetime(df["ds"])
    df["y"] = df["y"].astype(float)
    return df


# ---------------------------------------------------------------------------
# Step 3 -- naive / moving-average baseline
# ---------------------------------------------------------------------------

def naive_baseline_forecast(train_df: pd.DataFrame, horizon: int) -> pd.Series:
    """
    Baseline per docs/build-plan.md: a 7-day trailing moving average of the
    training series, carried forward flat as the prediction for every day
    in the holdout horizon (a naive model has no mechanism to predict a
    trend change, so a flat carry-forward is the honest naive forecast).
    """
    trailing_average = train_df["y"].tail(7).mean()
    return pd.Series([trailing_average] * horizon)

def evaluate_rolling_windows(df: pd.DataFrame) -> dict:
    """
    Evaluate Prophet and the naive baseline over three
    non-overlapping 7-calendar-day rolling windows.

    Each window:
    - Uses only observations before the window begins for training.
    - Predicts on the exact dates with actual reported prices.
    - Requires at least 180 training observations.
    - Requires at least 2 actual observations in the window.

    Errors are pooled across all three windows.
    """

    prophet_errors = []
    baseline_errors = []
    actual_values = []

    window_details = {}

    latest_date = pd.Timestamp(df["ds"].max()).normalize()

    for window_number in range(ROLLING_WINDOWS):

        # Most recent window first, then move backwards.
        window_end = latest_date - pd.Timedelta(
            days=window_number * FORECAST_HORIZON_DAYS
        )

        window_start = window_end - pd.Timedelta(
            days=FORECAST_HORIZON_DAYS - 1
        )

        # Only historical observations before the forecast window.
        train_df = df[df["ds"] < window_start].copy()

        # Actual observations within the 7-calendar-day window.
        holdout_df = df[
            (df["ds"] >= window_start) &
            (df["ds"] <= window_end)
        ].copy()

        # Ensure sufficient training history.
        if len(train_df) < MIN_HISTORY_DAYS:
            return {
                "status": "skipped",
                "reason": (
                    f"Window {window_number + 1}: "
                    f"only {len(train_df)} training observations; "
                    f"{MIN_HISTORY_DAYS} required."
                )
            }

        # Ensure sufficient actual observations.
        if len(holdout_df) < MIN_HOLDOUT_OBSERVATIONS_PER_WINDOW:
            return {
                "status": "skipped",
                "reason": (
                    f"Window {window_number + 1}: "
                    f"only {len(holdout_df)} holdout observations; "
                    f"{MIN_HOLDOUT_OBSERVATIONS_PER_WINDOW} required."
                )
            }

        # ---- Prophet evaluation ----
        eval_model = Prophet()
        eval_model.fit(train_df)

        future = holdout_df[["ds"]].copy()

        forecast = eval_model.predict(future)

        # Match actual and predicted prices by date.
        evaluation = holdout_df[["ds", "y"]].merge(
            forecast[["ds", "yhat"]],
            on="ds",
            how="left",
            validate="one_to_one"
        )

        evaluation = evaluation.sort_values("ds")

        if len(evaluation) != len(holdout_df):
            raise ValueError(
                f"Window {window_number + 1}: "
                "prediction count does not match actual observations."
            )

        if evaluation["yhat"].isna().any():
            raise ValueError(
                f"Window {window_number + 1}: missing Prophet predictions."
            )

        # ---- Naive baseline ----
        baseline_forecast = naive_baseline_forecast(
            train_df,
            len(evaluation)
        ).to_numpy()

        actual = evaluation["y"].to_numpy()
        predicted = evaluation["yhat"].to_numpy()

        prophet_window_errors = abs(predicted - actual)
        baseline_window_errors = abs(baseline_forecast - actual)

        prophet_mae_window = float(prophet_window_errors.mean())
        baseline_mae_window = float(baseline_window_errors.mean())

        # Collect errors for pooled evaluation.
        prophet_errors.extend(prophet_window_errors.tolist())
        baseline_errors.extend(baseline_window_errors.tolist())
        actual_values.extend(actual.tolist())

        # Save individual window metrics for diagnostics.
        window_details[f"window_{window_number + 1}_observations"] = len(evaluation)

        window_details[f"window_{window_number + 1}_start"] = (
            window_start.strftime("%Y-%m-%d")
        )

        window_details[f"window_{window_number + 1}_end"] = (
            window_end.strftime("%Y-%m-%d")
        )

        window_details[f"window_{window_number + 1}_prophet_mae"] = (
            prophet_mae_window
        )

        window_details[f"window_{window_number + 1}_baseline_mae"] = (
            baseline_mae_window
        )

    # ---- Aggregate all three windows ----

    prophet_mae = float(sum(prophet_errors) / len(prophet_errors))

    baseline_mae = float(sum(baseline_errors) / len(baseline_errors))

    mean_holdout_price = float(sum(actual_values) / len(actual_values))

    if mean_holdout_price <= 0:
        raise ValueError("Mean holdout price must be positive.")

    prophet_nmae_pct = (
        prophet_mae / mean_holdout_price
    ) * 100

    baseline_nmae_pct = (
        baseline_mae / mean_holdout_price
    ) * 100

    selected_method = (
        "prophet" if prophet_mae < baseline_mae else "naive"
    )

    return {
        "status": "evaluated",
        "prophet_mae": prophet_mae,
        "baseline_mae": baseline_mae,
        "prophet_nmae_pct": prophet_nmae_pct,
        "baseline_nmae_pct": baseline_nmae_pct,
        "selected_method": selected_method,
        "beats_baseline": prophet_mae < baseline_mae,
        "windows_evaluated": ROLLING_WINDOWS,
        "holdout_observations": len(actual_values),
        **window_details
    }


# ---------------------------------------------------------------------------
# Step 4 -- train + evaluate one (commodity, market) pair
# ---------------------------------------------------------------------------

def train_one_pair(commodity_id: int, market_id: int, commodity_name: str, market_name: str) -> dict:
    """
    Runs the full pipeline for a single pair. Opens its own DB connection
    since this function runs inside a parallel worker.

    Returns a result dict (always, even on failure) so the caller can
    report successes/failures without one bad pair killing the whole run.
    """
    label = f"{commodity_name} @ {market_name}"

    try:
        with engine.connect() as conn:
            df = load_series(commodity_id, market_id, conn)

        if len(df) < MIN_HISTORY_DAYS:
            # Can happen if eligibility was computed on distinct dates but
            # the averaged series (e.g. after a join quirk) ends up shorter;
            # guard again here rather than trust the earlier count blindly.
            return {
                "commodity_id": commodity_id,
                "market_id": market_id,
                "label": label,
                "status": "skipped",
                "reason": f"only {len(df)} days after loading, below minimum",
            }

        # ---- rolling-window evaluation ----
        evaluation_result = evaluate_rolling_windows(df)

        if evaluation_result["status"] != "evaluated":
            return {
                "commodity_id": commodity_id,
                "market_id": market_id,
                "commodity_name": commodity_name,
                "label": label,
                "status": "skipped",
                "reason": evaluation_result["reason"],
                "num_days": len(df),
            }

        prophet_mae = evaluation_result["prophet_mae"]
        baseline_mae = evaluation_result["baseline_mae"]

        prophet_nmae_pct = evaluation_result["prophet_nmae_pct"]
        baseline_nmae_pct = evaluation_result["baseline_nmae_pct"]

        selected_method = evaluation_result["selected_method"]

        # ---- full-data training (only when evaluation-only mode is disabled) ----
        model_path = None
        status = "evaluated"

        if not EVALUATION_ONLY:

            final_model = Prophet()
            final_model.fit(df)

            model_path = MODELS_DIR / f"{commodity_id}_{market_id}.pkl"

            dump(final_model, model_path)

            status = "trained"

        return {
            "commodity_id": commodity_id,
            "market_id": market_id,
            "commodity_name": commodity_name,
            "label": label,
            "status": status,
            "num_days": len(df),
            "prophet_mae": prophet_mae,
            "baseline_mae": baseline_mae,
            "prophet_nmae_pct": prophet_nmae_pct,
            "baseline_nmae_pct": baseline_nmae_pct,
            "selected_method": selected_method,
            "beats_baseline": prophet_mae < baseline_mae,
            "windows_evaluated": evaluation_result["windows_evaluated"],
            "holdout_observations": evaluation_result["holdout_observations"],
            **{
                k: v for k, v in evaluation_result.items()
                if k.startswith("window_")
            },
            "model_path": str(model_path) if model_path else None,
        }

        # ---- fit Prophet on the training portion, evaluate on holdout ----
        eval_model = Prophet()
        eval_model.fit(train_df)

        # ---- predict on the exact holdout dates ----
        future = holdout_df[["ds"]].copy()
        forecast = eval_model.predict(future)

        # Match actual and predicted values by date.
        evaluation = holdout_df[["ds", "y"]].merge(
           forecast[["ds", "yhat"]],
           on="ds",
           how="left",
           validate="one_to_one"
        )

        # Verify that every holdout observation has exactly one prediction.
        if len(evaluation) != HOLDOUT_DAYS:
            raise ValueError(
                f"Expected {HOLDOUT_DAYS} holdout observations, "
                f"but matched {len(evaluation)}."
            )

        if evaluation["yhat"].isna().any():
            raise ValueError("Missing Prophet predictions for one or more holdout dates.")

        # Sort by date before extracting arrays.
        evaluation = evaluation.sort_values("ds")

        predicted_holdout = evaluation["yhat"].to_numpy()
        actual_holdout = evaluation["y"].to_numpy()

        prophet_mae = float(abs(predicted_holdout - actual_holdout).mean())

        # ---- baseline over the same holdout window ----
        baseline_forecast = naive_baseline_forecast(
            train_df, HOLDOUT_DAYS
        ).to_numpy()

        baseline_mae = float(abs(baseline_forecast - actual_holdout).mean())

        # ---- normalized MAE ----
        # Express errors as a percentage of the mean actual holdout price.
        mean_holdout_price = float(evaluation["y"].mean())

        if mean_holdout_price <= 0:
            raise ValueError("Mean holdout price must be positive.")

        prophet_nmae_pct = (prophet_mae / mean_holdout_price) * 100
        baseline_nmae_pct = (baseline_mae / mean_holdout_price) * 100

        # ---- select the better method ----
        selected_method = (
            "prophet" if prophet_mae < baseline_mae else "naive"
        )

        # ---- full-data training (only when evaluation-only mode is disabled) ----
        model_path = None
        status = "evaluated"

        if not EVALUATION_ONLY:
            final_model = Prophet()
            final_model.fit(df)

            model_path = MODELS_DIR / f"{commodity_id}_{market_id}.pkl"
            dump(final_model, model_path)

            status = "trained"

        return {
            "commodity_id": commodity_id,
            "market_id": market_id,
            "commodity_name": commodity_name,
            "label": label,
            "status": status,
            "num_days": len(df),
            "prophet_mae": prophet_mae,
            "baseline_mae": baseline_mae,
            "prophet_nmae_pct": prophet_nmae_pct,
            "baseline_nmae_pct": baseline_nmae_pct,
            "selected_method": selected_method,
            "beats_baseline": prophet_mae < baseline_mae,
            "model_path": str(model_path) if model_path else None,
        }

    except Exception as exc:
        return {
            "commodity_id": commodity_id,
            "market_id": market_id,
            "label": label,
            "status": "failed",
            "reason": f"{type(exc).__name__}: {exc}",
        }


# ---------------------------------------------------------------------------
# Step 5 -- register successfully trained models
# ---------------------------------------------------------------------------

def register_models(results: list[dict]) -> int:
    """
    Upserts one model_registry row per successfully trained pair.
    Only ever called with status == "trained" results.
    """
    trained = [r for r in results if r["status"] == "trained"]

    if not trained:
        return 0

    with engine.begin() as conn:
        for r in trained:
            statement = pg_insert(model_registry).values(
                commodity_id=r["commodity_id"],
                market_id=r["market_id"],
                model_path=r["model_path"],
                holdout_mae=r["prophet_mae"],
                is_active=True,
            )
            statement = statement.on_conflict_do_update(
                index_elements=[
                    model_registry.c.commodity_id,
                    model_registry.c.market_id,
                ],
                set_={
                    "model_path": statement.excluded.model_path,
                    "holdout_mae": statement.excluded.holdout_mae,
                    "trained_at": func.now(),
                    "is_active": True,
                },
            )
            conn.execute(statement)

    return len(trained)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    start_time = time.time()

    with engine.connect() as conn:
        pairs_df = find_eligible_pairs(conn)

    if pairs_df.empty:
        print("No pairs meet the minimum history requirement. Nothing to train.")
        sys.exit(0)

    print("=" * 70)
    print(f"STEP 2-4 -- TRAINING {len(pairs_df):,} MODELS (parallel, n_jobs={N_JOBS})")
    print("=" * 70)

    jobs = [
        delayed(train_one_pair)(
            row.commodity_id, row.market_id, row.commodity_name, row.market_name
        )
        for row in pairs_df.itertuples(index=False)
    ]

    results = Parallel(n_jobs=2,prefer="threads",verbose=10)(jobs)

    evaluated = [r for r in results if r["status"] == "evaluated"]
    trained = [r for r in results if r["status"] == "trained"]
    skipped = [r for r in results if r["status"] == "skipped"]
    failed = [r for r in results if r["status"] == "failed"]

    successful = evaluated + trained

    # ---- database registration ----
    print("\n" + "=" * 70)
    print("STEP 5 -- MODEL REGISTRATION")
    print("=" * 70)

    if EVALUATION_ONLY:
        print("Evaluation-only mode: database registration skipped.")
    else:
        num_registered = register_models(results)
        print(f"Rows upserted into model_registry: {num_registered:,}")

    # ---- export evaluation results ----
    if successful:
        results_df = pd.DataFrame(successful)

        report_path = Path(__file__).resolve().parent / "evaluation_results.csv"

        results_df.to_csv(report_path, index=False)

        print(f"\nDetailed evaluation report saved to: {report_path}")

    # ---- export skipped pairs and their reasons ----
        if skipped:
            skipped_df = pd.DataFrame(skipped)

            skipped_path = (
                Path(__file__).resolve().parent / "evaluation_skipped_results.csv"
            )

            skipped_df.to_csv(skipped_path, index=False)

            print(f"\nSkipped-pair report saved to: {skipped_path}")

    # ---- summary ----
    print("\n" + "=" * 70)
    print("TRAINING / EVALUATION SUMMARY")
    print("=" * 70)

    print(f"Pairs attempted    : {len(results):,}")
    print(f"Evaluated         : {len(evaluated):,}")
    print(f"Full models trained: {len(trained):,}")
    print(f"Skipped           : {len(skipped):,}")
    print(f"Failed            : {len(failed):,}")

    if successful:

        prophet_wins = [
            r for r in successful
            if r["selected_method"] == "prophet"
        ]

        naive_wins = [
            r for r in successful
            if r["selected_method"] == "naive"
        ]

        print("\nMODEL SELECTION")
        print("-" * 40)
        print(f"Prophet selected: {len(prophet_wins):,}")
        print(f"Naive selected  : {len(naive_wins):,}")

        print("\nAVERAGE ERROR")
        print("-" * 40)

        avg_prophet_mae = sum(
            r["prophet_mae"] for r in successful
        ) / len(successful)

        avg_baseline_mae = sum(
            r["baseline_mae"] for r in successful
        ) / len(successful)

        avg_prophet_nmae = sum(
            r["prophet_nmae_pct"] for r in successful
        ) / len(successful)

        avg_baseline_nmae = sum(
            r["baseline_nmae_pct"] for r in successful
        ) / len(successful)

        print(f"Average Prophet MAE : {avg_prophet_mae:.2f}")
        print(f"Average Naive MAE   : {avg_baseline_mae:.2f}")

        print(f"Average Prophet NMAE: {avg_prophet_nmae:.2f}%")
        print(f"Average Naive NMAE  : {avg_baseline_nmae:.2f}%")

        # ---- commodity-wise breakdown ----
        print("\nCOMMODITY-WISE PERFORMANCE")
        print("-" * 70)

        results_df = pd.DataFrame(successful)

        commodity_summary = (
            results_df.groupby("commodity_name")
            .agg(
                markets=("market_id", "count"),
                prophet_nmae=("prophet_nmae_pct", "mean"),
                naive_nmae=("baseline_nmae_pct", "mean"),
                prophet_wins=(
                    "selected_method",
                    lambda x: (x == "prophet").sum()
                ),
                naive_wins=(
                    "selected_method",
                    lambda x: (x == "naive").sum()
                ),
            )
            .reset_index()
        )

        print(
            commodity_summary.to_string(
                index=False,
                float_format=lambda x: f"{x:.2f}"
            )
        )

    if failed:
        print("\nFAILED PAIRS (first 10)")
        print("-" * 40)

        for r in failed[:10]:
            print(f"{r['label']}: {r['reason']}")

    elapsed = time.time() - start_time
    print(f"\nTotal time: {elapsed:.1f}s")