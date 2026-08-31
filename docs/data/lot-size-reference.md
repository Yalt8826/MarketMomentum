# Lot-size reference (`data/reference/lot_sizes.csv`)

**Phase 4 (AGOR-66) of the GEX/DEX project.** Bhavcopy reports `OPEN_INT` and
`CONTRACTS` in **contracts**; converting to share exposure requires the market
lot, which NSE revises periodically. This file is the historical per-symbol
lot-size schedule for the dataset window **2021-08-02 → 2026-08-28**, shipped
as a change-point table and loaded via `mktmtm.data.lots.load_lot_sizes()`
(see `src/mktmtm/data/lots.py`, mirroring the `ADJUSTED_MIFOR.csv` pattern in
`src/mktmtm/data/rates.py`).

## Format

```
symbol,effective_from,lot_size,source
NIFTY,2021-08-02,50,nse_circular
RELIANCE,2021-08-02,250,derived
RELIANCE,2024-10-28,500,nse_bhavcopy
```

* `effective_from` — first **trade date** the lot applies (for `nse_circular`
  rows this is the circular's effective date for new contracts; for stock
  rows it is the first date the new lot dominates traded volume, i.e. the
  circular effective date plus the far-month phase-in).
* `lot_size` — contracts → shares multiplier for that symbol on that date.
* `source` — `nse_circular` (index symbols, values from NSE lot-revision
  circulars), `nse_bhavcopy` (value pinned by NSE's own per-contract
  `NewBrdLotQty` column in UDiFF bhavcopy, 2024-07-08+), or `derived`
  (reconstructed from bhavcopy value/volume and validated).

Lookup semantics: the lot in effect on date *d* is the row with the greatest
`effective_from ≤ d`. Dates before a symbol's first row have no lot (`None`).

## Route taken

Task 4.1 (preferred) — *ingest the historical lot-size schedule* — with two
official NSE anchors and one reconstruction:

1. **Index symbols** (`NIFTY`, `BANKNIFTY`, `FINNIFTY`, `MIDCPNIFTY`,
   `NIFTYNXT50`): exact values and effective dates from NSE circulars,
   verified 2026-09-01 — FAOP/47854 (2021-03-31), FAOP/56233 (2023-03-31),
   FAOP/61415 (2024-04-02), FAOP/64625+64672+64990 (2024-10/11),
   FAOP/67372 (2025-03-28), FAOP/70616 (2025-10-03). `NIFTYFPI` (launched
   2025-06-11, appears in the bhavcopy only from 2026-08-12) takes its lot
   from the bhavcopy column.
2. **Stock revision dates**: the official quarterly "Revision in Market Lot
   of Derivative Contracts on Individual Stocks" circulars (FAOP/49795 …
   FAOP/73549) fix *when* lots may change; the effective date is announced
   ~1 month after each circular (far-month phase-in).
3. **Stock lot values**: reconstructed per symbol-day from the bhavcopy's own
   traded value. Legacy `VAL_INLAKH` and UDiFF `TtlTrfVal` are the
   (strike+premium)-basis traded value in lakhs/rupees; the ratio to
   `vol × (strike + settle)` is the lot size to ~1%. The expiring series is
   excluded on its expiry day (its value column switches basis — this was the
   only systematic bias found). Every stock step is validated against the
   circular-date windows, and wherever NSE's per-contract `NewBrdLotQty`
   column (2024-07-08+) is unambiguous on a plateau, the plateau value is
   taken from it exactly (`nse_bhavcopy`).

## Validation (2026-09-01 build)

* **Coverage**: 251,519 / 251,521 symbol-days (100.00%) have a schedule lot.
* **Derived vs schedule**: 98.75% of symbol-days with a volume signal are
  within 3% of the schedule lot; 98.90% outside the 35-day phase-in windows.
* **Step dates**: 510 of 548 stock steps (93.1%) fall inside a known
  quarterly-revision window; the remaining 38 are corporate-action lot
  adjustments (bonus/split), which are real lot changes confirmed by the
  official column where it exists (e.g. HDFCBANK 550→1100→550 in 2025,
  ASHOKLEY 2500→5000 in 2025).
* Spot checks against well-known revisions: TCS 300→150 (2021-10-29),
  SBIN 1500→750 (2024-04-26), MRF 10→5 (2023-10-27), ITC 3200→1600
  (2022-10-28), IRCTC 1625→875 (2021-10-29), RELIANCE 250→500 (2024-10-28),
  NIFTY 75→65 (2025-10-28) — all present with matching dates.

## Caveats

* **Phase-in windows.** Revisions apply to *new* contracts from the effective
  date; existing expiries keep the old lot until they expire. Inside a
  phase-in window (≈35–60 calendar days after a step) different expiries of
  the same symbol trade with different lots, so a single per-symbol lot is an
  approximation (volume-weighted mix). The phase-in is visible in the raw
  data as `official_uniq ≥ 2` in the UDiFF files.
* **Legacy-only plateaus** (no official column overlap) carry the derived
  value rounded to the nearest integer; a systematic value-vs-settle bias of
  ~0.1% can leave an off-by-one on large lots (e.g. ITC 3199 vs 3200 before
  2022-10-28). Impact on exposure is <0.1%.
* **Corporate actions.** Lot changes driven by bonus/split adjustments are
  included (they are real contract-lot changes) but are not tied to the
  quarterly circular list.

## Rebuilding

```bash
uv run python scripts/build_lot_size_reference.py   # on athena, repo root
```

The builder re-parses `data/raw/bhavcopy/extracted/`, recomputes the
per-symbol schedule, validates it, and rewrites the CSV plus a summary.
NSE circular data used as anchors is documented in the script header.
