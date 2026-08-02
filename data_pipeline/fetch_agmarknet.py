"""
Daily live ingestion from the data.gov.in Agmarknet API.
Resource ID: 9ef84268-d588-465a-a308-a864a43d0070
Run on a schedule (see .github/workflows/daily_pipeline.yml).

TODO:
- Load API key from environment variable (AGMARKNET_API_KEY) — never hardcode it
- Fetch latest day's records for the scoped states/commodities
- Handle the variety/grade duplicate-row gotcha (see docs/build-plan.md, Section 3.0)
- Append cleaned rows into the state-partitioned DB tables (see db.py)
"""

def fetch_daily_prices():
    raise NotImplementedError("Phase 1 — implement live daily fetch")


if __name__ == "__main__":
    fetch_daily_prices()
