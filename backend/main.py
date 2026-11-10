"""
FastAPI backend — see docs/build-plan.md, Section 6.

Serves per-mandi forecasts AND state-level summary endpoints. The
state-summary endpoint AGGREGATES existing per-mandi forecasts on the
fly (Section 4.5) — it does not use a separately trained state-level
model.

/forecast reads from the precomputed `forecasts` table (written by
ml/generate_forecasts.py) rather than loading a .pkl and calling
.predict() live on every request — see that script's module docstring
for why this matters (resolves a real inconsistency between an earlier
project decision and build-plan.md's own draft code).

Top-6-crops-per-state is hardcoded here (FINALIZED_CROPS) rather than
read from a `state_top_crops` DB table. build-plan.md references such a
table in two places, but it was never actually added to db.py's schema
— and since the crop selection is already finalized (Section 3.3.1,
data-driven, locked), a config table for data that won't change again
is unneeded overhead for this project's scope. If per-state crop lists
ever need to change dynamically, promote this constant to a real table
then.
"""

import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data_pipeline"))
from db import (  # noqa: E402
    engine,
    states,
    districts,
    markets,
    commodities,
    forecasts,
    price_records,
)


app = FastAPI(title="Live Mandi Price Advisor API")

# Frontend (Vite dev server) runs on a different origin during development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to the real frontend origin before any real deployment
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Finalized scope — see module docstring for why this is hardcoded here
# rather than a DB table. Must stay in sync with
# data_pipeline/backfill_kaggle.py's FINALIZED_CROPS if that ever changes.
# ---------------------------------------------------------------------------

FINALIZED_CROPS = {
    "Tamil Nadu": [
        "Coconut", "Bhindi (Ladies Finger)", "Green Chilli",
        "Bottle gourd", "Snakeguard", "Onion",
    ],
    "Uttar Pradesh": [
        "Potato", "Onion", "Tomato", "Wheat", "Brinjal", "Green Chilli",
    ],
    "Maharashtra": [
        "Wheat", "Bengal Gram (Gram)(Whole)", "Soyabean",
        "Arhar (Tur/Red Gram)(Whole)", "Onion", "Jowar (Sorghum)",
    ],
}


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def _get_state_id(state_name: str, conn) -> int:
    row = conn.execute(
        select(states.c.id).where(states.c.name == state_name)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown state: {state_name}")
    return row


def _get_commodity_id(commodity_name: str, conn) -> int:
    row = conn.execute(
        select(commodities.c.id).where(commodities.c.name == commodity_name)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown commodity: {commodity_name}")
    return row


def _get_market_id(market_name: str, conn) -> int:
    row = conn.execute(
        select(markets.c.id).where(markets.c.name == market_name)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown market: {market_name}")
    return row


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/states")
def list_states():
    """Powers the clickable India map on the Prediction page. Scoped to
    the 3 finalized states only (not every state in the lookup table —
    though with the current backfill scope these are the only states
    present anyway)."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(states.c.id, states.c.name)
            .where(states.c.name.in_(FINALIZED_CROPS.keys()))
            .order_by(states.c.name)
        ).all()

    return [{"id": r.id, "name": r.name} for r in rows]


@app.get("/states/{state}/top-crops")
def top_crops(state: str):
    """Returns the finalized top-6-crop list for a state. See module
    docstring for why this reads a hardcoded constant, not a DB table."""
    if state not in FINALIZED_CROPS:
        raise HTTPException(status_code=404, detail=f"Unknown state: {state}")
    return {"state": state, "top_crops": FINALIZED_CROPS[state]}


@app.get("/states/{state}/summary")
def state_summary(state: str):
    """
    Aggregates existing precomputed forecasts for this state's top-6
    crops — one row per (commodity, market) pair with its latest
    forecast and signal. This is arithmetic over already-computed
    forecasts, not a separately trained state-level model (Section 4.5).
    """
    if state not in FINALIZED_CROPS:
        raise HTTPException(status_code=404, detail=f"Unknown state: {state}")

    crop_names = FINALIZED_CROPS[state]

    with engine.connect() as conn:
        state_id = _get_state_id(state, conn)

        # Markets belonging to this state, joined through districts.
        state_markets = select(markets.c.id).join(
            districts, districts.c.id == markets.c.district_id
        ).where(districts.c.state_id == state_id)

        statement = (
            select(
                commodities.c.name.label("commodity"),
                markets.c.name.label("market"),
                forecasts.c.forecast_date,
                forecasts.c.predicted_price,
                forecasts.c.lower_bound,
                forecasts.c.upper_bound,
                forecasts.c.sell_hold_signal,
            )
            .join(commodities, commodities.c.id == forecasts.c.commodity_id)
            .join(markets, markets.c.id == forecasts.c.market_id)
            .where(
                commodities.c.name.in_(crop_names),
                forecasts.c.market_id.in_(state_markets),
            )
            .order_by(commodities.c.name, markets.c.name, forecasts.c.forecast_date)
        )

        rows = conn.execute(statement).all()

    results = [
        {
            "commodity": r.commodity,
            "market": r.market,
            "forecast_date": str(r.forecast_date),
            "predicted_price": float(r.predicted_price) if r.predicted_price is not None else None,
            "lower_bound": float(r.lower_bound) if r.lower_bound is not None else None,
            "upper_bound": float(r.upper_bound) if r.upper_bound is not None else None,
            "sell_hold_signal": r.sell_hold_signal,
        }
        for r in rows
    ]

    sell_count = sum(1 for r in results if r["sell_hold_signal"] == "SELL")
    hold_count = sum(1 for r in results if r["sell_hold_signal"] == "HOLD")

    return {
        "state": state,
        "top_crops": crop_names,
        "forecasts": results,
        "sell_count": sell_count,
        "hold_count": hold_count,
    }


@app.get("/forecast")
def get_forecast(commodity: str, market: str):
    """
    Returns the precomputed forecast horizon for one (commodity, market)
    pair, read from the `forecasts` table — NOT a live Prophet call.
    See module docstring and ml/generate_forecasts.py for why.
    """
    with engine.connect() as conn:
        commodity_id = _get_commodity_id(commodity, conn)
        market_id = _get_market_id(market, conn)

        statement = (
            select(
                forecasts.c.forecast_date,
                forecasts.c.predicted_price,
                forecasts.c.lower_bound,
                forecasts.c.upper_bound,
                forecasts.c.sell_hold_signal,
                forecasts.c.generated_at,
            )
            .where(
                forecasts.c.commodity_id == commodity_id,
                forecasts.c.market_id == market_id,
            )
            .order_by(forecasts.c.forecast_date)
        )

        rows = conn.execute(statement).all()

    if not rows:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No precomputed forecast found for {commodity} @ {market}. "
                "Has ml/train.py + ml/generate_forecasts.py been run for this pair?"
            ),
        )

    return {
        "commodity": commodity,
        "market": market,
        "forecast": [
            {
                "date": str(r.forecast_date),
                "predicted_price": float(r.predicted_price) if r.predicted_price is not None else None,
                "lower_bound": float(r.lower_bound) if r.lower_bound is not None else None,
                "upper_bound": float(r.upper_bound) if r.upper_bound is not None else None,
            }
            for r in rows
        ],
        # Same signal for every row in the horizon (one badge per pair) --
        # see ml/generate_forecasts.py.
        "signal": rows[0].sell_hold_signal,
        "generated_at": str(rows[0].generated_at),
    }


@app.get("/compare")
def compare_mandis(commodity: str, state: str):
    """
    Per-mandi comparison for the drill-down view: latest known actual
    modal_price for this commodity, across every mandi in the given
    state (not forecasts -- this view is about current actual prices
    side by side, matching build-plan.md's description).
    """
    with engine.connect() as conn:
        commodity_id = _get_commodity_id(commodity, conn)
        state_id = _get_state_id(state, conn)

        # Latest arrival_date on record for this commodity within this state.
        latest_date = conn.execute(
            select(price_records.c.arrival_date)
            .where(
                price_records.c.commodity_id == commodity_id,
                price_records.c.state_id == state_id,
            )
            .order_by(price_records.c.arrival_date.desc())
            .limit(1)
        ).scalar_one_or_none()

        if latest_date is None:
            raise HTTPException(
                status_code=404,
                detail=f"No price records found for {commodity} in {state}",
            )

        statement = (
            select(
                markets.c.name.label("market"),
                price_records.c.modal_price,
            )
            .join(markets, markets.c.id == price_records.c.market_id)
            .where(
                price_records.c.commodity_id == commodity_id,
                price_records.c.state_id == state_id,
                price_records.c.arrival_date == latest_date,
            )
            .order_by(price_records.c.modal_price.desc())
        )

        rows = conn.execute(statement).all()

    return {
        "commodity": commodity,
        "state": state,
        "as_of_date": str(latest_date),
        "markets": [
            {"market": r.market, "modal_price": float(r.modal_price) if r.modal_price is not None else None}
            for r in rows
        ],
    }

@app.get("/history")
def get_history(commodity: str, market: str):
    """
    Returns variety-averaged daily modal prices for one commodity-market
    pair, ordered from earliest to latest arrival date.
    """
    with engine.connect() as conn:
        statement = (
            select(
                price_records.c.arrival_date.label("date"),
                func.avg(price_records.c.modal_price).label("price"),
            )
            .join(markets, markets.c.id == price_records.c.market_id)
            .join(commodities, commodities.c.id == price_records.c.commodity_id)
            .where(
                commodities.c.name == commodity,
                markets.c.name == market,
            )
            .group_by(price_records.c.arrival_date)
            .order_by(price_records.c.arrival_date.asc())
        )
        rows = conn.execute(statement).all()

    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No historical price records found for {commodity} @ {market}.",
        )

    return {
        "commodity": commodity,
        "market": market,
        "history": [
            {
                "date": str(row.date),
                "price": float(row.price) if row.price is not None else None,
            }
            for row in rows
        ],
    }

