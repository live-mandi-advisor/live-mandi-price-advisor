from sqlalchemy.dialects.postgresql import insert

from db import engine, price_records
from backfill_kaggle import map_to_ids

import pandas as pd
import numpy as np
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------

# PROJECT CONFIGURATION

# ---------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Load environment variables from the project root.

load_dotenv(PROJECT_ROOT / ".env")

AGMARKNET_API_KEY = os.getenv("AGMARKNET_API_KEY")

API_URL = (
"https://api.data.gov.in/resource/"
"9ef84268-d588-465a-a308-a864a43d0070"
)

API_TIMEOUT = 90
API_PAGE_SIZE = 1000

# ---------------------------------------------------------

# PROJECT DATA SCOPE

# ---------------------------------------------------------

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

# ---------------------------------------------------------

# API CONFIGURATION VALIDATION

# ---------------------------------------------------------

def validate_api_configuration():
    if not AGMARKNET_API_KEY:
        raise EnvironmentError(
            "AGMARKNET_API_KEY is missing. "
            "Check your project's .env file."
        )

    print("API configuration loaded successfully.")
    print("Resource ID: 9ef84268-d588-465a-a308-a864a43d0070")
    print(f"States configured: {len(FINALIZED_CROPS)}")
    print(
        "State-commodity combinations: "
        f"{sum(len(crops) for crops in FINALIZED_CROPS.values())}"
    )

def fetch_api_records(state, commodity):
    all_records = []
    offset = 0
    total_records = None

    headers = {
        "Accept": "application/json",
        "User-Agent": "curl/8.22.0"
    }

    while True:
        params = {
            "api-key": AGMARKNET_API_KEY,
            "format": "json",
            "limit": API_PAGE_SIZE,
            "offset": offset,
            "filters[state.keyword]": state,
            "filters[commodity]": commodity
        }

        try:
            response = requests.get(
                API_URL,
                params=params,
                headers=headers,
                timeout=API_TIMEOUT
            )

            response.raise_for_status()
            payload = response.json()

        except requests.exceptions.RequestException as error:
            raise RuntimeError(
                f"API request failed for {state} - {commodity}: "
                f"{type(error).__name__}"
            ) from error

        except ValueError as error:
            raise RuntimeError(
                f"API returned invalid JSON for {state} - {commodity}."
            ) from error

        page_records = payload.get("records", [])

        if not isinstance(page_records, list):
            raise ValueError("Unexpected API response: records is not a list.")

        try:
            current_total = int(payload["total"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                "API response does not contain a valid total record count."
            ) from error

        if total_records is None:
            total_records = current_total
        elif current_total != total_records:
            raise ValueError(
                "API total changed during pagination. "
                "Retry this state-commodity combination."
            )

        all_records.extend(page_records)

        print(
            f"{state} | {commodity} | "
            f"Fetched {len(all_records)}/{total_records}"
        )

        if not page_records or len(all_records) >= total_records:
            break

        offset += len(page_records)

    return all_records

def normalize_api_records(records):
    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)

    column_mapping = {
        "state": "State",
        "district": "District",
        "market": "Market",
        "commodity": "Commodity",
        "variety": "Variety",
        "arrival_date": "Arrival_Date",
        "min_price": "Min_Price",
        "max_price": "Max_Price",
        "modal_price": "Modal_Price"
    }

    df = df.rename(columns=column_mapping)

    required_columns = list(column_mapping.values())

    missing_columns = [
        column for column in required_columns
        if column not in df.columns
    ]

    if missing_columns:
        raise ValueError(
            f"Missing required API columns: {missing_columns}"
        )

    df = df[required_columns].copy()

    # Convert prices to numeric values
    price_columns = ["Min_Price", "Max_Price", "Modal_Price"]

    for column in price_columns:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    # Convert API date format (DD/MM/YYYY) to Python datetime
    df["Arrival_Date"] = pd.to_datetime(
        df["Arrival_Date"],
        format="%d/%m/%Y",
        errors="coerce"
    )

    return df

def clean_live_records(df):
    if df.empty:
        return df.copy()

    df = df.copy()

    initial_count = len(df)

    # Remove rows with missing essential information
    required_columns = [
        "State",
        "District",
        "Market",
        "Commodity",
        "Variety",
        "Arrival_Date"
    ]

    df = df.dropna(subset=required_columns)

    # Remove invalid or non-positive prices
    price_columns = ["Min_Price", "Max_Price", "Modal_Price"]

    df = df.dropna(subset=price_columns)

    for column in price_columns:
        df = df[df[column] > 0]

    # Ensure price ordering is valid
    df = df[
        (df["Min_Price"] <= df["Modal_Price"]) &
        (df["Modal_Price"] <= df["Max_Price"])
    ]

    # Remove duplicate natural keys
    natural_key = [
        "State",
        "District",
        "Market",
        "Commodity",
        "Variety",
        "Arrival_Date"
    ]

    duplicate_count = df.duplicated(
        subset=natural_key,
        keep="first"
    ).sum()

    df = df.drop_duplicates(
        subset=natural_key,
        keep="first"
    )

    df = df.reset_index(drop=True)

    print("\nLive data cleaning report")
    print("-------------------------")
    print("Initial records:", initial_count)
    print("Final records:", len(df))
    print("Records removed:", initial_count - len(df))
    print("Duplicate records detected:", duplicate_count)

    return df

def insert_live_records(df, conn):
    if df.empty:
        print("No live records available for insertion.")
        return 0

    # Resolve lookup IDs using the existing backfill function
    mapped_df = map_to_ids(df, conn)

    # Prepare database records
    records = []

    for row in mapped_df.itertuples(index=False):
        records.append({
            "state_id": int(row.state_id),
            "market_id": int(row.market_id),
            "commodity_id": int(row.commodity_id),
            "variety_id": int(row.variety_id),
            "arrival_date": row.Arrival_Date.date(),
            "min_price": float(row.Min_Price),
            "max_price": float(row.Max_Price),
            "modal_price": float(row.Modal_Price),
            "source": 1
        })

    # Insert without overwriting existing records
    statement = (
        insert(price_records)
        .values(records)
        .on_conflict_do_nothing(
            index_elements=[
                price_records.c.market_id,
                price_records.c.commodity_id,
                price_records.c.variety_id,
                price_records.c.arrival_date
            ]
        )
    )

    result = conn.execute(statement)

    inserted_count = result.rowcount

    print("\nLive database insertion report")
    print("--------------------------------")
    print("Prepared records:", len(records))
    print("Inserted records:", inserted_count)
    print("Skipped existing records:", len(records) - inserted_count)

    return inserted_count

if __name__ == "__main__":

    validate_api_configuration()

    total_combinations = 0
    total_fetched = 0
    total_cleaned = 0
    total_cross_batch_duplicates = 0

    seen_live_keys = set()
    all_cleaned_batches = []

    print("\nStarting 18-combination live data pipeline...")

    # ==========================================================
    # STAGE 1: FETCH AND CLEAN ALL 18 COMBINATIONS
    # ==========================================================

    for state, commodities in FINALIZED_CROPS.items():

        for commodity in commodities:

            total_combinations += 1

            print("\n" + "=" * 60)
            print(f"Processing: {state} | {commodity}")
            print("=" * 60)

            records = fetch_api_records(state, commodity)

            total_fetched += len(records)

            df = normalize_api_records(records)

            cleaned_df = clean_live_records(df)

            total_cleaned += len(cleaned_df)

            if cleaned_df.empty:
                continue

            # Remove duplicates across API batches

            key_columns = [
                "State",
                "District",
                "Market",
                "Commodity",
                "Variety",
                "Arrival_Date"
            ]

            batch_keys = list(
                cleaned_df[key_columns].itertuples(
                    index=False,
                    name=None
                )
            )

            keep_mask = [
                key not in seen_live_keys
                for key in batch_keys
            ]

            cross_batch_duplicates = (
                len(batch_keys) - sum(keep_mask)
            )

            total_cross_batch_duplicates += cross_batch_duplicates

            cleaned_df = cleaned_df.loc[keep_mask].copy()

            seen_live_keys.update(
                key
                for key, keep in zip(batch_keys, keep_mask)
                if keep
            )

            print(
                "Cross-batch duplicates removed:",
                cross_batch_duplicates
            )

            if not cleaned_df.empty:

                all_cleaned_batches.append(cleaned_df)

    # ==========================================================
    # STAGE 2: COMBINE ALL CLEANED RECORDS
    # ==========================================================

    if all_cleaned_batches:

        combined_df = pd.concat(
            all_cleaned_batches,
            ignore_index=True
        )

    else:

        combined_df = pd.DataFrame()

    print("\n" + "=" * 60)
    print("FETCHING AND CLEANING SUMMARY")
    print("=" * 60)

    print("Combinations processed:", total_combinations)
    print("Total records fetched:", total_fetched)
    print("Records after individual batch cleaning:", total_cleaned)
    print("Cross-batch duplicates removed:", total_cross_batch_duplicates)
    print("Final records prepared:", len(combined_df))

    # ==========================================================
    # STAGE 3: PRODUCTION DATABASE INSERTION
    # ==========================================================

    if combined_df.empty:
        raise RuntimeError(
            "No valid records were prepared. "
            "Database insertion cancelled."
        )

    print("\nStarting production database insertion...")

    try:

        # Automatically commits on success.
        # Automatically rolls back if an error occurs.

        with engine.begin() as conn:

            inserted_count = insert_live_records(
                combined_df,
                conn
            )

        skipped_count = len(combined_df) - inserted_count

        print("\n" + "=" * 60)
        print("LIVE DATABASE INSERTION SUMMARY")
        print("=" * 60)

        print("Records prepared:", len(combined_df))
        print("Records successfully inserted:", inserted_count)
        print("Records skipped (existing duplicates):", skipped_count)

        print("\nLive database insertion completed successfully.")

    except Exception as error:

        print("\nLIVE DATABASE INSERTION FAILED.")
        print(type(error).__name__, str(error))

        raise