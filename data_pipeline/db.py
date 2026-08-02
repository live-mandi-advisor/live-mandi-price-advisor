"""
Database access layer — tables partitioned by state
(see docs/build-plan.md, Section 4.5 for why: storage/UI is state-first,
but ML models stay per (commodity, mandi), never per state).

TODO:
- Connection setup (SQLite locally, Postgres/Supabase in production)
- Schema: state-partitioned price tables + model_registry + state_top_crops
"""
