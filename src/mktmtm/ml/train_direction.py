"""Training entry point for the XGBoost next-day direction model.

Orchestrates feature preparation, the sealed splits, model fitting, and
honest out-of-sample evaluation. No model is trained before Phases 1-4
(the evaluation harness) are complete.
"""
