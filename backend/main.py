"""
FastAPI backend — see docs/build-plan.md, Section 6.

Serves per-mandi forecasts AND state-level summary endpoints. The
state-summary endpoint AGGREGATES existing per-mandi models on the fly
(Section 4.5) — it does not use a separately trained state-level model.

TODO: implement each endpoint below.
"""
from fastapi import FastAPI

app = FastAPI(title="Live Mandi Price Advisor API")


@app.get("/states")
def list_states():
    """Powers the clickable India map on the Prediction page."""
    raise NotImplementedError


@app.get("/states/{state}/top-crops")
def top_crops(state: str):
    """Reads the precomputed state_top_crops table (Section 3.3.1)."""
    raise NotImplementedError


@app.get("/states/{state}/summary")
def state_summary(state: str):
    """Aggregates existing per-mandi models — see Section 4.5."""
    raise NotImplementedError


@app.get("/forecast")
def get_forecast(commodity: str, market: str):
    """Loads the individual (commodity, mandi) Prophet model and forecasts."""
    raise NotImplementedError


@app.get("/compare")
def compare_mandis(commodity: str, state: str):
    """Per-mandi comparison for the drill-down view."""
    raise NotImplementedError
