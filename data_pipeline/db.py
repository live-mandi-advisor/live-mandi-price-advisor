"""
Database access layer for the Live Mandi Price Advisor pipeline.

Schema (SQLAlchemy Core, not ORM): a single normalized `price_records`
fact table, with `state_id` as an indexed/filterable column — NOT
separate tables per state. Lookup tables (states, districts, markets,
commodities, varieties) normalize raw text values to IDs. `forecasts`
stores precomputed Prophet output (not calculated live per API request).
`model_registry` tracks the one active model per (commodity, market)
pair.

Connection: reads DATABASE_URL from .env (Supabase Postgres, direct/
session connection string, not the transaction pooler — see
docs/build-plan.md for why). Driver: psycopg2.

Run this file directly to create all tables and verify the connection:
    python data_pipeline/db.py
"""

import os
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.engine import Engine


# Load DATABASE_URL from .env
load_dotenv()

DATABASE_URL: Optional[str] = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL is not set in the .env file")


# SQLAlchemy engine for PostgreSQL/Supabase.
# psycopg2 is used as the PostgreSQL driver.
engine: Engine = create_engine(DATABASE_URL)


# Single SQLAlchemy Core metadata object.
metadata = MetaData()


# ---------------------------------------------------------------------------
# LOOKUP TABLES
# ---------------------------------------------------------------------------

# Stores the list of states.
states = Table(
    "states",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String, unique=True, nullable=False),
)


# Stores districts belonging to each state.
districts = Table(
    "districts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "state_id",
        Integer,
        ForeignKey("states.id"),
        nullable=False,
    ),
    Column("name", String, nullable=False),
    UniqueConstraint("state_id", "name"),
)


# Stores markets belonging to each district.
markets = Table(
    "markets",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "district_id",
        Integer,
        ForeignKey("districts.id"),
        nullable=False,
    ),
    Column("name", String, nullable=False),
    UniqueConstraint("district_id", "name"),
)


# Stores the list of commodities/crops.
commodities = Table(
    "commodities",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String, unique=True, nullable=False),
)


# Stores varieties belonging to each commodity.
varieties = Table(
    "varieties",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "commodity_id",
        Integer,
        ForeignKey("commodities.id"),
        nullable=False,
    ),
    Column("name", String, nullable=False),
    UniqueConstraint("commodity_id", "name"),
)


# ---------------------------------------------------------------------------
# FACT TABLE
# ---------------------------------------------------------------------------

# Stores historical and live mandi price records.
price_records = Table(
    "price_records",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "state_id",
        Integer,
        ForeignKey("states.id"),
        nullable=False,
    ),
    Column(
        "market_id",
        Integer,
        ForeignKey("markets.id"),
        nullable=False,
    ),
    Column(
        "commodity_id",
        Integer,
        ForeignKey("commodities.id"),
        nullable=False,
    ),
    Column(
        "variety_id",
        Integer,
        ForeignKey("varieties.id"),
        nullable=False,
    ),
    Column("arrival_date", Date, nullable=False),
    Column("min_price", Numeric(10, 2)),
    Column("max_price", Numeric(10, 2)),
    Column("modal_price", Numeric(10, 2)),
    # 0 = kaggle_backfill, 1 = live_api
    Column("source", Integer, nullable=False),

    # Natural deduplication key and future upsert conflict target.
    UniqueConstraint(
        "market_id",
        "commodity_id",
        "variety_id",
        "arrival_date",
    ),

    # Indexes for common filtering/query patterns.
    Index(
        "idx_price_records_state",
        "state_id",
    ),
    Index(
        "idx_price_records_commodity_market",
        "commodity_id",
        "market_id",
    ),
)


# ---------------------------------------------------------------------------
# FORECASTS TABLE
# ---------------------------------------------------------------------------

# Stores precomputed Prophet forecasts and the resulting Sell/Hold signal.
forecasts = Table(
    "forecasts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "commodity_id",
        Integer,
        ForeignKey("commodities.id"),
        nullable=False,
    ),
    Column(
        "market_id",
        Integer,
        ForeignKey("markets.id"),
        nullable=False,
    ),
    Column("forecast_date", Date, nullable=False),
    Column("predicted_price", Numeric(10, 2)),
    Column("lower_bound", Numeric(10, 2)),
    Column("upper_bound", Numeric(10, 2)),
    Column("sell_hold_signal", String),
    Column("generated_at", DateTime, default=text("now()")),

    # One forecast per commodity, market, and forecast date.
    UniqueConstraint(
        "commodity_id",
        "market_id",
        "forecast_date",
    ),
)


# ---------------------------------------------------------------------------
# MODEL REGISTRY TABLE
# ---------------------------------------------------------------------------

# Tracks the trained Prophet model associated with each commodity/market pair.
model_registry = Table(
    "model_registry",
    metadata,
    Column(
        "commodity_id",
        Integer,
        ForeignKey("commodities.id"),
        primary_key=True,
    ),
    Column(
        "market_id",
        Integer,
        ForeignKey("markets.id"),
        primary_key=True,
    ),
    Column("model_path", String, nullable=False),
    Column("trained_at", DateTime, default=text("now()")),
    Column("holdout_mae", Numeric),
    Column("is_active", Boolean, default=True),
)


def create_all_tables() -> None:
    """Create all defined tables if they do not already exist."""
    metadata.create_all(engine)


if __name__ == "__main__":
    try:
        # Ensure environment variables from .env are loaded.
        load_dotenv()

        # Create all tables safely.
        create_all_tables()

        # Simple connection test.
        with engine.connect() as connection:
            result = connection.execute(text("SELECT 1"))
            result.scalar_one()

        print("SUCCESS: Connected to PostgreSQL/Supabase and tables are ready.")

    except Exception as exc:
        print(f"FAILURE: Database setup or connection test failed: {exc}")