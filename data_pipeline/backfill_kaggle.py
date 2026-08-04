"""
One-time historical backfill from the Kaggle "Daily Market Prices of
Commodity India (2001-2026)" dataset — see docs/build-plan.md, Section 3.3.

Uses the 2024.parquet and 2025.parquet slices (same underlying Agmarknet
data as the live API, compiled by the same original dataset owner).
CEDA Agri Market Data is no longer used as a source (see build-plan.md
Section 3.3 for why).

Run ONCE to seed enough history for Prophet to learn weekly/yearly
seasonality, before daily live collection (fetch_agmarknet.py) takes over.

Scope (finalized, Section 3.3.1): 3 states x 6 crops each (18 total).
"""

FINALIZED_STATES = ["Tamil Nadu", "Uttar Pradesh", "Maharashtra"]

FINALIZED_CROPS = {
    "Tamil Nadu": [
        "Coconut", "Bhindi(Ladies Finger)", "Green Chilli",
        "Bottle gourd", "Snakeguard", "Onion",
    ],
    "Uttar Pradesh": [
        "Potato", "Onion", "Tomato", "Wheat", "Brinjal", "Green Chilli",
    ],
    "Maharashtra": [
        "Wheat", "Bengal Gram(Gram)(Whole)", "Soyabean",
        "Arhar (Tur/Red Gram)(Whole)", "Onion", "Jowar(Sorghum)",
    ],
}

# TODO Phase 1:
# - Read 2024.parquet and 2025.parquet
# - Filter to FINALIZED_STATES + FINALIZED_CROPS above
# - Clean/standardize commodity name variants (e.g. confirm exact string
#   spellings against the parquet's actual commodity column values --
#   names above are best-effort, verify before filtering)
# - Resolve the still-open Variety question (build-plan.md Section 3.3.1)
#   before finalizing per-(commodity, mandi) series
# - Load into the same state-partitioned tables used by fetch_agmarknet.py


def backfill_from_kaggle(parquet_paths: list[str]):
    raise NotImplementedError(
        "Phase 1 — implement Kaggle parquet historical backfill"
    )


if __name__ == "__main__":
    pass
