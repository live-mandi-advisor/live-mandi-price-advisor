"""
Trains one Prophet model per (commodity, mandi) pair — see docs/build-plan.md,
Section 4 and 4.5. Never trains a single combined "state-level" model.

TODO:
- Pull cleaned historical series per (commodity, market) from the DB
- Fit Prophet, compare against a naive/moving-average baseline (MAE)
- Save trained model via joblib to ml/models/{commodity}_{market}.pkl
- Register in model_registry table (see retrain.py for the champion/challenger check)
"""
