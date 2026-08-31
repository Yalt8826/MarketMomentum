"""Time-based train/valid/test splitting for the direction model.

Implements the three sealed splits (TRAIN 2021-08-02 -> 2024-12-31,
VALID 2025-01-01 -> 2025-12-31, TEST 2026-01-01 -> 2026-08-28) with
strict no-lookahead guarantees: 2026 data is never touched before Phase 7.
"""
