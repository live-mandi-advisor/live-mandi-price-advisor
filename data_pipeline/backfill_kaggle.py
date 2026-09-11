"""
One-time historical backfill from Kaggle 2024.parquet + 2025.parquet
into the Supabase Postgres schema defined in db.py.

Pipeline: load_raw -> filter_to_scope -> clean -> map_to_ids ->
upsert_price_records -> validate. Lookup tables (states, districts,
markets, commodities, varieties) are populated respecting the
state->district->market and commodity->variety hierarchy — get-or-
create is cached in-memory per run to avoid one query per row.

Grade is intentionally not part of the duplicate/upsert key (not
stored in price_records, not present in the live API either) —
grade-only duplicates are collapsed by keeping the first occurrence.

Run from the project root: python data_pipeline/backfill_kaggle.py
Requires 2024.parquet/2025.parquet at data/raw/, and DATABASE_URL
set in .env (see db.py).
"""

from pathlib import Path

import pandas as pd
from sqlalchemy import func, select, text
from sqlalchemy.engine import Connection

from sqlalchemy.dialects.postgresql import insert

from db import (
    engine,
    states,
    districts,
    markets,
    commodities,
    varieties,
    price_records,
)


# ---------------------------------------------------------------------------
# Project scope
# ---------------------------------------------------------------------------

FINALIZED_CROPS = {
    "Tamil Nadu": [
        "Coconut",
        "Bhindi (Ladies Finger)",
        "Green Chilli",
        "Bottle gourd",
        "Snakeguard",
        "Onion",
    ],
    "Uttar Pradesh": [
        "Potato",
        "Onion",
        "Tomato",
        "Wheat",
        "Brinjal",
        "Green Chilli",
    ],
    "Maharashtra": [
        "Wheat",
        "Bengal Gram (Gram)(Whole)",
        "Soyabean",
        "Arhar (Tur/Red Gram)(Whole)",
        "Onion",
        "Jowar (Sorghum)",
    ],
}


# ---------------------------------------------------------------------------
# Part 1 — Load raw parquet files
# ---------------------------------------------------------------------------

def load_raw(paths: list[str]) -> pd.DataFrame:
    """
    Read the supplied parquet files and concatenate them into one DataFrame.
    """

    print("\n" + "=" * 70)
    print("STAGE 1 — LOADING RAW HISTORICAL DATA")
    print("=" * 70)

    if not paths:
        raise ValueError("No parquet file paths were provided.")

    frames = []

    for path in paths:
        file_path = Path(path)

        print(f"\nReading: {file_path}")

        if not file_path.exists():
            raise FileNotFoundError(
                f"Parquet file not found: {file_path}"
            )

        frame = pd.read_parquet(file_path)

        print(f"  Rows loaded: {len(frame):,}")

        frames.append(frame)

    df = pd.concat(frames, ignore_index=True)

    print("\nCombined dataset")
    print("-" * 70)
    print(f"Total rows: {len(df):,}")

    print("\nColumns:")
    print(list(df.columns))

    print("\nData types:")
    print(df.dtypes)

    print("\nMissing values per column:")
    print(df.isnull().sum())

    print("\nRaw data loading completed.")

    return df


# ---------------------------------------------------------------------------
# Part 2 — Filter to finalized project scope
# ---------------------------------------------------------------------------

def filter_to_scope(df: pd.DataFrame) -> pd.DataFrame:
    """
    Filter the national historical dataset to the project's
    finalized 18 state-commodity combinations.
    """

    print("\n" + "=" * 70)
    print("STAGE 2 — FILTERING TO PROJECT SCOPE")
    print("=" * 70)

    original_count = len(df)

    # Before filtering, inspect the actual commodity spellings for each
    # finalized state. This makes spelling mismatches visible instead of
    # silently producing zero rows.
    print("\nActual commodity values found for each finalized state:")

    for state in FINALIZED_CROPS:
        state_rows = df.loc[df["State"] == state, "Commodity"]

        unique_commodities = sorted(
            state_rows.dropna().unique().tolist()
        )

        print(f"\n{state}:")
        if unique_commodities:
            for commodity in unique_commodities:
                print(f"  - {commodity}")
        else:
            print("  No rows found for this state.")

    # Build a boolean mask for the exact state + commodity combinations.
    mask = pd.Series(False, index=df.index)

    for state, crops in FINALIZED_CROPS.items():
        mask |= (
            (df["State"] == state)
            & df["Commodity"].isin(crops)
        )

    filtered_df = df.loc[mask].copy()

    print("\nFiltering summary")
    print("-" * 70)
    print(f"Original row count : {original_count:,}")
    print(f"Filtered row count : {len(filtered_df):,}")
    print(f"Rows removed       : {original_count - len(filtered_df):,}")

    print("\nRows per finalized state-commodity combination:")
    print("-" * 70)

    for state, crops in FINALIZED_CROPS.items():
        print(f"\n{state}")

        for commodity in crops:
            count = len(
                filtered_df[
                    (filtered_df["State"] == state)
                    & (filtered_df["Commodity"] == commodity)
                ]
            )

            print(f"  {commodity:<40} {count:>10,}")

    print("\nScope filtering completed.")

    return filtered_df


# ---------------------------------------------------------------------------
# Part 3 — Clean historical data
# ---------------------------------------------------------------------------

def clean(
    df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Clean the scoped historical price data before database insertion.

    Cleaning rules:
    1. Convert zero/negative prices to NaN.
    2. Drop rows without a usable modal price.
    3. Remove structurally invalid price relationships.
    4. Remove extreme/suspicious modal-price anomalies.
    5. Parse Arrival_Date as datetime.
    6. Remove duplicate natural-key records.
    """

    df = df.copy()

    rows_before = len(df)

    # ------------------------------------------------------------------
    # 1. Convert zero/negative prices to NaN
    # ------------------------------------------------------------------
    price_columns = [
        "Min_Price",
        "Max_Price",
        "Modal_Price",
    ]

    invalid_price_cells = 0

    for col in price_columns:
        invalid_mask = df[col] <= 0
        invalid_price_cells += int(invalid_mask.sum())
        df.loc[invalid_mask, col] = pd.NA

    print("\nInvalid price values converted to NaN:")
    print(f"  Total invalid price cells: {invalid_price_cells:,}")

    # ------------------------------------------------------------------
    # 2. Modal price is the forecasting target, so it must exist
    # ------------------------------------------------------------------
    missing_modal_before = int(df["Modal_Price"].isna().sum())

    df = df.dropna(subset=["Modal_Price"]).copy()

    print("\nRows dropped because Modal_Price was missing:")
    print(f"  {missing_modal_before:,}")

    # ------------------------------------------------------------------
    # 3. Remove structurally invalid price relationships
    #
    # A valid record should satisfy:
    #     Min_Price <= Modal_Price <= Max_Price
    # ------------------------------------------------------------------
    structural_invalid_mask = (
        (df["Min_Price"] > df["Max_Price"])
        | (df["Modal_Price"] < df["Min_Price"])
        | (df["Modal_Price"] > df["Max_Price"])
    )

    structural_invalid_count = int(structural_invalid_mask.sum())

    df = df.loc[~structural_invalid_mask].copy()

    print("\nStructurally invalid price rows removed:")
    print(f"  {structural_invalid_count:,}")
    print("  Rule: Min_Price <= Modal_Price <= Max_Price")

    # ------------------------------------------------------------------
    # 4. Remove extreme/suspicious modal-price anomalies
    #
    # This catches:
    #   - extremely large corrupted values
    #   - unusually large modal prices relative to the minimum price
    # ------------------------------------------------------------------
    anomaly_mask = (
        (df["Modal_Price"] > 100_000)
        | (
            (df["Modal_Price"] > 25_000)
            & (df["Modal_Price"] > df["Min_Price"] * 5)
        )
    )

    anomaly_count = int(anomaly_mask.sum())

    df = df.loc[~anomaly_mask].copy()

    print("\nExtreme/suspicious price anomalies removed:")
    print(f"  {anomaly_count:,}")
    print(
        "  Rule: Modal_Price > 100,000 OR "
        "(Modal_Price > 25,000 AND Modal_Price > 5 × Min_Price)"
    )

    # ------------------------------------------------------------------
    # 5. Parse Arrival_Date
    # ------------------------------------------------------------------
    df["Arrival_Date"] = pd.to_datetime(
        df["Arrival_Date"],
        format="%Y-%m-%d",
    )

    # ------------------------------------------------------------------
    # 6. Remove duplicate natural-key records
    #
    # Same key used by the price_records UNIQUE constraint:
    # Market + Commodity + Variety + Arrival_Date
    # ------------------------------------------------------------------
    duplicate_mask = df.duplicated(
        subset=[
            "Market",
            "Commodity",
            "Variety",
            "Arrival_Date",
        ],
        keep="first",
    )

    duplicate_rows_removed = int(duplicate_mask.sum())
    duplicate_groups = int(
        df.loc[duplicate_mask, [
            "Market",
            "Commodity",
            "Variety",
            "Arrival_Date",
        ]].drop_duplicates().shape[0]
    )

    df = df.loc[~duplicate_mask].copy()

    print("\nDuplicate natural-key records removed:")
    print(f"  Duplicate groups: {duplicate_groups:,}")
    print(f"  Rows removed    : {duplicate_rows_removed:,}")
    print("  Rule            : keep first occurrence")

    # ------------------------------------------------------------------
    # Final summary
    # ------------------------------------------------------------------
    print("\nCleaning summary")
    print("-" * 70)
    print(f"Rows before cleaning                  : {rows_before:,}")
    print(f"Invalid price cells → NaN             : {invalid_price_cells:,}")
    print(f"Rows dropped for missing Modal_Price   : {missing_modal_before:,}")
    print(f"Structurally invalid rows removed     : {structural_invalid_count:,}")
    print(f"Price anomaly rows removed            : {anomaly_count:,}")
    print(f"Duplicate groups collapsed            : {duplicate_groups:,}")
    print(f"Duplicate rows removed                : {duplicate_rows_removed:,}")
    print(f"Final clean row count                 : {len(df):,}")

    print("\nArrival_Date dtype after parsing:")
    print(f"  {df['Arrival_Date'].dtype}")

    print("\nData cleaning completed.")

    return df


# ---------------------------------------------------------------------------
# Part 4 — Map text values to normalized database IDs
# ---------------------------------------------------------------------------

def map_to_ids(
    df: pd.DataFrame,
    conn: Connection,
) -> pd.DataFrame:
    """
    Get or create normalized lookup rows and map the original text
    values to their database IDs.

    Hierarchy:
        state
          └── district
                └── market

        commodity
          └── variety

    Cache keys always include the relevant parent ID where required.
    """

    print("\n" + "=" * 70)
    print("STAGE 4 — NORMALIZING LOOKUP VALUES")
    print("=" * 70)

    df = df.copy()

    # ------------------------------------------------------------------
    # In-memory caches.
    #
    # The parent ID is deliberately part of the key for hierarchical
    # values. This prevents collisions such as:
    #
    #   (state_id=1, district="X")
    #   (state_id=2, district="X")
    #
    # being treated as the same district.
    # ------------------------------------------------------------------

    state_cache: dict[str, int] = {}
    district_cache: dict[tuple[int, str], int] = {}
    market_cache: dict[tuple[int, str], int] = {}
    commodity_cache: dict[str, int] = {}
    variety_cache: dict[tuple[int, str], int] = {}

    def require_text(value, column_name: str) -> str:
        """Ensure a lookup value is present and usable as text."""

        if pd.isna(value):
            raise ValueError(
                f"Missing value in required lookup column "
                f"'{column_name}'."
            )

        return str(value)

    def get_or_create_state(name: str) -> int:
        if name in state_cache:
            return state_cache[name]

        statement = (
            select(states.c.id)
            .where(states.c.name == name)
        )

        existing_id = conn.execute(statement).scalar_one_or_none()

        if existing_id is not None:
            state_id = int(existing_id)
        else:
            insert_statement = (
                insert(states)
                .values(name=name)
                .returning(states.c.id)
            )

            state_id = int(
                conn.execute(insert_statement).scalar_one()
            )

        state_cache[name] = state_id

        return state_id

    def get_or_create_district(
        state_id: int,
        name: str,
    ) -> int:
        cache_key = (state_id, name)

        if cache_key in district_cache:
            return district_cache[cache_key]

        statement = (
            select(districts.c.id)
            .where(
                districts.c.state_id == state_id,
                districts.c.name == name,
            )
        )

        existing_id = conn.execute(statement).scalar_one_or_none()

        if existing_id is not None:
            district_id = int(existing_id)
        else:
            insert_statement = (
                insert(districts)
                .values(
                    state_id=state_id,
                    name=name,
                )
                .returning(districts.c.id)
            )

            district_id = int(
                conn.execute(insert_statement).scalar_one()
            )

        district_cache[cache_key] = district_id

        return district_id

    def get_or_create_market(
        district_id: int,
        name: str,
    ) -> int:
        cache_key = (district_id, name)

        if cache_key in market_cache:
            return market_cache[cache_key]

        statement = (
            select(markets.c.id)
            .where(
                markets.c.district_id == district_id,
                markets.c.name == name,
            )
        )

        existing_id = conn.execute(statement).scalar_one_or_none()

        if existing_id is not None:
            market_id = int(existing_id)
        else:
            insert_statement = (
                insert(markets)
                .values(
                    district_id=district_id,
                    name=name,
                )
                .returning(markets.c.id)
            )

            market_id = int(
                conn.execute(insert_statement).scalar_one()
            )

        market_cache[cache_key] = market_id

        return market_id

    def get_or_create_commodity(name: str) -> int:
        if name in commodity_cache:
            return commodity_cache[name]

        statement = (
            select(commodities.c.id)
            .where(commodities.c.name == name)
        )

        existing_id = conn.execute(statement).scalar_one_or_none()

        if existing_id is not None:
            commodity_id = int(existing_id)
        else:
            insert_statement = (
            insert(commodities)
            .values(name=name)
            .returning(commodities.c.id)
            )

            commodity_id = int(
            conn.execute(insert_statement).scalar_one()
            )
        commodity_cache[name] = commodity_id

        return commodity_id

    def get_or_create_variety(
        commodity_id: int,
        name: str,
    ) -> int:
        cache_key = (commodity_id, name)

        if cache_key in variety_cache:
            return variety_cache[cache_key]

        statement = (
            select(varieties.c.id)
            .where(
                varieties.c.commodity_id == commodity_id,
                varieties.c.name == name,
            )
        )

        existing_id = conn.execute(statement).scalar_one_or_none()

        if existing_id is not None:
            variety_id = int(existing_id)
        else:
            insert_statement = (
                insert(varieties)
                .values(
                    commodity_id=commodity_id,
                    name=name,
                )
                .returning(varieties.c.id)
            )

            variety_id = int(
                conn.execute(insert_statement).scalar_one()
            )

        variety_cache[cache_key] = variety_id

        return variety_id

    # ------------------------------------------------------------------
    # Only unique lookup combinations are processed here.
    # This is much cheaper than querying the database for every
    # historical price row.
    # ------------------------------------------------------------------

    lookup_columns = [
        "State",
        "District",
        "Market",
        "Commodity",
        "Variety",
    ]

    unique_lookups = (
        df[lookup_columns]
        .drop_duplicates()
        .copy()
    )

    print(
        f"\nUnique lookup combinations to process: "
        f"{len(unique_lookups):,}"
    )

    # Store resolved IDs in a mapping keyed by the full hierarchy.
    resolved_ids: dict[
        tuple[str, str, str, str, str],
        tuple[int, int, int, int, int],
    ] = {}

    for row in unique_lookups.itertuples(index=False):
        state_name = require_text(row.State, "State")
        district_name = require_text(row.District, "District")
        market_name = require_text(row.Market, "Market")
        commodity_name = require_text(row.Commodity, "Commodity")
        variety_name = require_text(row.Variety, "Variety")

        state_id = get_or_create_state(state_name)

        district_id = get_or_create_district(
            state_id,
            district_name,
        )

        market_id = get_or_create_market(
            district_id,
            market_name,
        )

        commodity_id = get_or_create_commodity(
            commodity_name,
        )

        variety_id = get_or_create_variety(
            commodity_id,
            variety_name,
        )

        key = (
            state_name,
            district_name,
            market_name,
            commodity_name,
            variety_name,
        )

        resolved_ids[key] = (
            state_id,
            district_id,
            market_id,
            commodity_id,
            variety_id,
        )

    # ------------------------------------------------------------------
    # Apply the resolved IDs back to every price row.
    # ------------------------------------------------------------------

    id_values = []

    for row in df.itertuples(index=False):
        key = (
            row.State,
            row.District,
            row.Market,
            row.Commodity,
            row.Variety,
        )

        id_values.append(resolved_ids[key])

    id_df = pd.DataFrame(
        id_values,
        columns=[
            "state_id",
            "district_id",
            "market_id",
            "commodity_id",
            "variety_id",
        ],
        index=df.index,
    )

    for column in id_df.columns:
        df[column] = id_df[column]

    print("\nLookup normalization summary")
    print("-" * 70)
    print(f"States cached/created      : {len(state_cache):,}")
    print(f"Districts cached/created   : {len(district_cache):,}")
    print(f"Markets cached/created     : {len(market_cache):,}")
    print(f"Commodities cached/created : {len(commodity_cache):,}")
    print(f"Varieties cached/created   : {len(variety_cache):,}")

    print("\nNormalized IDs added to DataFrame:")
    print("  state_id")
    print("  district_id")
    print("  market_id")
    print("  commodity_id")
    print("  variety_id")

    print("\nLookup normalization completed.")

    return df


# ---------------------------------------------------------------------------
# Part 5 — Upsert price records
# ---------------------------------------------------------------------------

def upsert_price_records(
    df: pd.DataFrame,
    conn: Connection,
) -> int:
    """
    Insert historical price records into price_records.

    Existing records with the same natural database key are updated
    instead of duplicated.
    """

    print("\n" + "=" * 70)
    print("STAGE 5 — UPSERTING HISTORICAL PRICE RECORDS")
    print("=" * 70)

    if df.empty:
        print("\nNo rows to upsert.")
        return 0

    required_columns = [
        "state_id",
        "market_id",
        "commodity_id",
        "variety_id",
        "Arrival_Date",
        "Min_Price",
        "Max_Price",
        "Modal_Price",
    ]

    missing_columns = [
        column
        for column in required_columns
        if column not in df.columns
    ]

    if missing_columns:
        raise ValueError(
            f"Missing required columns before database upsert: "
            f"{missing_columns}"
        )

    # Convert pandas NaN/NaT values into Python None so PostgreSQL
    # receives SQL NULL rather than an invalid numeric value.
    records = []

    for row in df.itertuples(index=False):
        min_price = row.Min_Price
        max_price = row.Max_Price
        modal_price = row.Modal_Price

        records.append(
            {
                "state_id": int(row.state_id),
                "market_id": int(row.market_id),
                "commodity_id": int(row.commodity_id),
                "variety_id": int(row.variety_id),
                "arrival_date": row.Arrival_Date.date(),
                "min_price": (
                    None if pd.isna(min_price)
                    else float(min_price)
                ),
                "max_price": (
                    None if pd.isna(max_price)
                    else float(max_price)
                ),
                "modal_price": (
                    None if pd.isna(modal_price)
                    else float(modal_price)
                ),
                "source": 0,
            }
        )

    # A few thousand rows per database round-trip keeps memory usage
    # reasonable while avoiding one INSERT per historical record.
    batch_size = 1000

    total_upserted = 0
    total_batches = (
        (len(records) + batch_size - 1) // batch_size
    )

    print(f"\nRows prepared for upsert: {len(records):,}")
    print(f"Batch size              : {batch_size:,}")
    print(f"Number of batches       : {total_batches:,}")

    for batch_number, start in enumerate(
        range(0, len(records), batch_size),
        start=1,
    ):
        batch = records[start:start + batch_size]

        # PostgreSQL-specific INSERT.
        statement = insert(price_records).values(batch)

        # Conflict target matches the UNIQUE constraint in db.py.
        statement = statement.on_conflict_do_update(
            index_elements=[
                price_records.c.market_id,
                price_records.c.commodity_id,
                price_records.c.variety_id,
                price_records.c.arrival_date,
            ],
            set_={
                "min_price": statement.excluded.min_price,
                "max_price": statement.excluded.max_price,
                "modal_price": statement.excluded.modal_price,
                "source": statement.excluded.source,
            },
        )

        result = conn.execute(statement)

        # PostgreSQL reports the number of rows affected by the
        # INSERT/UPDATE statement.
        batch_count = result.rowcount or 0
        total_upserted += batch_count

        print(
            f"  Batch {batch_number:>4}/{total_batches:<4} "
            f"→ {batch_count:,} rows processed"
        )

    print("\nDatabase upsert completed.")
    print(f"Total rows upserted/updated: {total_upserted:,}")

    return total_upserted


# ---------------------------------------------------------------------------
# Part 6 — Validate database contents
# ---------------------------------------------------------------------------

def validate() -> None:
    """
    Run post-backfill sanity checks against PostgreSQL.
    """

    print("\n" + "=" * 70)
    print("STAGE 6 — DATABASE VALIDATION")
    print("=" * 70)

    with engine.connect() as conn:

        lookup_tables = [
            ("states", states),
            ("districts", districts),
            ("markets", markets),
            ("commodities", commodities),
            ("varieties", varieties),
        ]

        print("\nLookup table row counts")
        print("-" * 70)

        for table_name, table in lookup_tables:
            count = conn.execute(
                select(func.count()).select_from(table)
            ).scalar_one()

            print(f"{table_name:<15} : {count:,}")

        # ---------------------------------------------------------------
        # Total price_records
        # ---------------------------------------------------------------

        price_count = conn.execute(
            select(func.count()).select_from(price_records)
        ).scalar_one()

        print("\nPrice records")
        print("-" * 70)
        print(f"Total rows: {price_count:,}")

        # ---------------------------------------------------------------
        # Date range
        # ---------------------------------------------------------------

        date_result = conn.execute(
            select(
                func.min(price_records.c.arrival_date),
                func.max(price_records.c.arrival_date),
            )
        ).one()

        min_date, max_date = date_result

        print(f"Minimum arrival_date: {min_date}")
        print(f"Maximum arrival_date: {max_date}")

        # ---------------------------------------------------------------
        # Invalid modal prices
        # ---------------------------------------------------------------

        invalid_modal_count = conn.execute(
            select(func.count())
            .select_from(price_records)
            .where(
                (price_records.c.modal_price <= 0)
                | price_records.c.modal_price.is_(None)
            )
        ).scalar_one()

        print(
            "\nRows with invalid/missing modal_price: "
            f"{invalid_modal_count:,}"
        )

        # ---------------------------------------------------------------
        # Duplicate natural database keys
        # ---------------------------------------------------------------

        duplicate_subquery = (
            select(
                price_records.c.market_id,
                price_records.c.commodity_id,
                price_records.c.variety_id,
                price_records.c.arrival_date,
                func.count().label("record_count"),
            )
            .group_by(
                price_records.c.market_id,
                price_records.c.commodity_id,
                price_records.c.variety_id,
                price_records.c.arrival_date,
            )
            .having(func.count() > 1)
            .subquery()
        )

        duplicate_group_count = conn.execute(
            select(func.count())
            .select_from(duplicate_subquery)
        ).scalar_one()

        print(
            "Duplicate natural-key groups: "
            f"{duplicate_group_count:,}"
        )

        # ---------------------------------------------------------------
        # Overall validation status
        # ---------------------------------------------------------------

        validation_passed = (
            invalid_modal_count == 0
            and duplicate_group_count == 0
        )

        print("\nValidation result")
        print("-" * 70)

        if validation_passed:
            print("PASS — Database sanity checks completed successfully.")
        else:
            print("WARNING — One or more sanity checks require attention.")

    print("\nDatabase validation completed.")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    print("\n")
    print("=" * 70)
    print("LIVE MANDI PRICE ADVISOR")
    print("KAGGLE HISTORICAL DATA BACKFILL PIPELINE")
    print("=" * 70)

    try:
        # ---------------------------------------------------------------
        # Locate the two historical parquet files.
        # Running from the project root is expected:
        #
        #     python data_pipeline/backfill_kaggle.py
        # ---------------------------------------------------------------

        data_dir = Path("data/raw")

        parquet_paths = [
            str(data_dir / "2024.parquet"),
            str(data_dir / "2025.parquet"),
        ]

        # ---------------------------------------------------------------
        # Stage 1 — Load
        # ---------------------------------------------------------------

        df = load_raw(parquet_paths)

        # ---------------------------------------------------------------
        # Stage 2 — Filter
        # ---------------------------------------------------------------

        df = filter_to_scope(df)

        # ---------------------------------------------------------------
        # Stage 3 — Clean
        # ---------------------------------------------------------------

        df = clean(df)

        # ---------------------------------------------------------------
        # Stage 4 + 5 — Database work.
        #
        # engine.begin() provides one transaction for lookup creation
        # and price-record upserts. If an exception occurs, the
        # transaction is rolled back.
        # ---------------------------------------------------------------

        with engine.begin() as conn:

            df = map_to_ids(
                df,
                conn,
            )

            upserted_count = upsert_price_records(
                df,
                conn,
            )

        # ---------------------------------------------------------------
        # Stage 6 — Validate after the write transaction has committed.
        # ---------------------------------------------------------------

        validate()

        # ---------------------------------------------------------------
        # Final summary
        # ---------------------------------------------------------------

        print("\n" + "=" * 70)
        print("BACKFILL COMPLETED SUCCESSFULLY")
        print("=" * 70)
        print(f"Final cleaned rows : {len(df):,}")
        print(f"Rows upserted      : {upserted_count:,}")
        print("Database            : Supabase PostgreSQL")
        print("Source              : Kaggle 2024 + 2025")
        print("Scope               : 3 states × 6 commodities = 18 combinations")
        print("=" * 70)

    except Exception as exc:

        print("\n" + "=" * 70)
        print("BACKFILL FAILED")
        print("=" * 70)
        print(f"Error: {exc}")
        print("=" * 70)

        raise