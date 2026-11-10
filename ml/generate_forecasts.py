"""
Precomputes 7-day-ahead forecasts for every actively trained model and
writes them into the `forecasts` table.

Why this is a separate script from train.py: db.py already splits
"a model exists" (model_registry) from "here's what it predicts"
(forecasts, keyed by forecast_date). Training is comparatively slow and
only needs to happen when new data arrives; generating forward forecasts
from an already-trained model is cheap and is what the backend actually
reads from (see note below on the precompute-vs-live inconsistency this
resolves).

Run this AFTER train.py has populated model_registry and saved .pkl
files to ml/models/.

Design note (resolves a real inconsistency found 9 Nov 2026): an earlier
project decision was "precompute forecasts rather than calculating
forecasts live when the API receives a request," but the /forecast
endpoint example in docs/build-plan.md actually called model.predict()
live, on every request. This script implements the precompute decision:
backend/main.py's /forecast endpoint should do a plain SELECT against
the `forecasts` table this script fills, not load a .pkl and call
.predict() itself.

Sell/Hold rule (not previously defined anywhere in docs/build-plan.md --
only "rule-based" was decided, no concrete rule): compare the forecasted
price on the LAST day of the horizon against the last known actual
price.
    - forecast trending up  (above last actual, beyond a small noise
      margin)      -> HOLD (price likely to improve, advise waiting)
    - forecast flat or falling (at/below last actual)
                   -> SELL (price unlikely to improve, advise selling now)
This is a simple, explainable starting rule, easy to defend in a viva
and easy to replace later without touching anything else.
"""

import sys
import time
from pathlib import Path

import pandas as pd
from joblib import Parallel, delayed, load
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data_pipeline"))
from db import engine, model_registry, price_records, forecasts  # noqa: E402


FORECAST_HORIZON_DAYS = 7  # matches the project's confirmed forecast horizon

# A forecast within this fraction of the last known price counts as "flat",
# not a real up/down move -- avoids a HOLD/SELL flip caused by Prophet's own
# noise on an essentially unchanged price.
FLAT_BAND = 0.02  # 2%

N_JOBS = -1

MODELS_DIR = Path(__file__).resolve().parent / "models"


def get_active_models(conn) -> pd.DataFrame:
    """One row per model_registry entry currently marked active."""
    statement = select(
        model_registry.c.commodity_id,
        model_registry.c.market_id,
        model_registry.c.model_path,
    ).where(model_registry.c.is_active.is_(True))

    rows = conn.execute(statement).all()
    return pd.DataFrame(rows, columns=["commodity_id", "market_id", "model_path"])


def get_last_known_price(commodity_id: int, market_id: int, conn) -> float | None:
    """
    Last actual modal_price on record for this pair, averaged across
    varieties on that date -- same convention train.py uses when building
    the training series, so the Sell/Hold comparison is apples-to-apples
    with what the model was trained on.
    """
    statement = (
        select(price_records.c.arrival_date, price_records.c.modal_price)
        .where(
            price_records.c.commodity_id == commodity_id,
            price_records.c.market_id == market_id,
        )
        .order_by(price_records.c.arrival_date.desc())
        .limit(50)  # enough rows to average the most recent date's varieties
    )
    rows = conn.execute(statement).all()
    if not rows:
        return None

    most_recent_date = rows[0][0]
    same_day_prices = [float(p) for d, p in rows if d == most_recent_date and p is not None]
    if not same_day_prices:
        return None

    return sum(same_day_prices) / len(same_day_prices)


def compute_sell_hold(forecast_last_day_price: float, last_known_price: float) -> str:
    """See module docstring for the rule and why it was chosen."""
    if last_known_price == 0:
        return "HOLD"  # degenerate case, avoid divide-by-zero; flag rather than guess

    pct_change = (forecast_last_day_price - last_known_price) / last_known_price

    if pct_change > FLAT_BAND:
        return "HOLD"
    return "SELL"


def forecast_one_pair(commodity_id: int, market_id: int, model_path: str) -> dict:
    """
    Loads a trained model, forecasts FORECAST_HORIZON_DAYS ahead, and
    returns the rows ready to upsert plus the resulting signal.
    Opens its own connection since this runs inside a parallel worker.
    """
    try:
        model = load(model_path)

        future = model.make_future_dataframe(periods=FORECAST_HORIZON_DAYS)
        forecast = model.predict(future)
        horizon = forecast.tail(FORECAST_HORIZON_DAYS)[
            ["ds", "yhat", "yhat_lower", "yhat_upper"]
        ]

        with engine.connect() as conn:
            last_known_price = get_last_known_price(commodity_id, market_id, conn)

        if last_known_price is None:
            return {
                "commodity_id": commodity_id,
                "market_id": market_id,
                "status": "skipped",
                "reason": "no price history found for last-known-price comparison",
            }

        last_day_forecast = float(horizon["yhat"].iloc[-1])
        signal = compute_sell_hold(last_day_forecast, last_known_price)

        rows = []
        for _, r in horizon.iterrows():
            rows.append(
                {
                    "commodity_id": commodity_id,
                    "market_id": market_id,
                    "forecast_date": r["ds"].date(),
                    "predicted_price": float(r["yhat"]),
                    "lower_bound": float(r["yhat_lower"]),
                    "upper_bound": float(r["yhat_upper"]),
                    # Same signal applied across the whole horizon for this
                    # pair -- one Sell/Hold badge per (commodity, mandi), not
                    # per day, matching how the frontend displays it
                    # (build-plan.md Section 7/8: one badge per mandi).
                    "sell_hold_signal": signal,
                }
            )

        return {
            "commodity_id": commodity_id,
            "market_id": market_id,
            "status": "forecasted",
            "signal": signal,
            "rows": rows,
        }

    except Exception as exc:
        return {
            "commodity_id": commodity_id,
            "market_id": market_id,
            "status": "failed",
            "reason": f"{type(exc).__name__}: {exc}",
        }


def write_forecasts(results: list[dict]) -> int:
    """Upserts all forecast rows from successful results in one transaction."""
    all_rows = []
    for r in results:
        if r["status"] == "forecasted":
            all_rows.extend(r["rows"])

    if not all_rows:
        return 0

    with engine.begin() as conn:
        statement = pg_insert(forecasts).values(all_rows)
        statement = statement.on_conflict_do_update(
            index_elements=[
                forecasts.c.commodity_id,
                forecasts.c.market_id,
                forecasts.c.forecast_date,
            ],
            set_={
                "predicted_price": statement.excluded.predicted_price,
                "lower_bound": statement.excluded.lower_bound,
                "upper_bound": statement.excluded.upper_bound,
                "sell_hold_signal": statement.excluded.sell_hold_signal,
                # generated_at intentionally left to its own default --
                # re-generating a forecast should still stamp "now", which
                # the column default already provides on insert; an
                # ON CONFLICT UPDATE path does not re-run the default, so
                # set it explicitly here to the current time.
                "generated_at": func.now(),
            },
        )
        conn.execute(statement)

    return len(all_rows)


if __name__ == "__main__":
    start_time = time.time()

    with engine.connect() as conn:
        active_models = get_active_models(conn)

    if active_models.empty:
        print("No active models in model_registry. Run train.py first.")
        sys.exit(0)

    print("=" * 70)
    print(f"GENERATING {FORECAST_HORIZON_DAYS}-DAY FORECASTS FOR {len(active_models):,} ACTIVE MODELS")
    print("=" * 70)

    jobs = [
        delayed(forecast_one_pair)(row.commodity_id, row.market_id, row.model_path)
        for row in active_models.itertuples(index=False)
    ]

    results = Parallel(n_jobs=N_JOBS, verbose=10, prefer="threads")(jobs)

    forecasted = [r for r in results if r["status"] == "forecasted"]
    skipped = [r for r in results if r["status"] == "skipped"]
    failed = [r for r in results if r["status"] == "failed"]

    num_rows_written = write_forecasts(results)

    sell_count = sum(1 for r in forecasted if r["signal"] == "SELL")
    hold_count = sum(1 for r in forecasted if r["signal"] == "HOLD")

    print("\n" + "=" * 70)
    print("FORECAST GENERATION SUMMARY")
    print("=" * 70)
    print(f"Pairs attempted     : {len(results):,}")
    print(f"Forecasted          : {len(forecasted):,}")
    print(f"Skipped (no history): {len(skipped):,}")
    print(f"Failed              : {len(failed):,}")
    print(f"Forecast rows written to DB: {num_rows_written:,}")
    print(f"SELL signals: {sell_count:,}  |  HOLD signals: {hold_count:,}")

    if failed:
        print("\nFailed pairs (first 10):")
        for r in failed[:10]:
            print(f"  commodity_id={r['commodity_id']} market_id={r['market_id']}: {r['reason']}")

    elapsed = time.time() - start_time
    print(f"\nTotal time: {elapsed:.1f}s")