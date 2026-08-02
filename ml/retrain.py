"""
Scheduled retraining job — runs daily after fetch_agmarknet.py.
Refits each (commodity, mandi) model on the full accumulated history
(historical backfill + all live days collected so far).

Champion/challenger check: a newly retrained model only replaces the
live one if its holdout error is no worse — see docs/build-plan.md.

TODO: implement the retrain loop + model_registry update
"""
