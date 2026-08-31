"""Feature engineering for the next-day direction model.

Builds the cross-sectional feature matrix consumed by the XGBoost ranker:
the 47 option-derived features from ``ml_dataset``, prepared for the
train/valid/test windows.
"""
