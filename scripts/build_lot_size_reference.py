"""Build ``data/reference/lot_sizes.csv`` — the historical F&O lot-size schedule.

Bhavcopy reports ``OPEN_INT``/``CONTRACTS`` in **contracts**; share exposure
needs the lot size, which NSE revises periodically. This script reconstructs
the per-symbol schedule and validates it against official NSE data:

* **Indices** (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50, NIFTYFPI):
  hard-coded from NSE lot-revision circulars (verified 2026-09-01):
  FAOP/47854 (2021-03-31), FAOP/56233 (2023-03-31), FAOP/61415 (2024-04-02),
  FAOP/64625+64672+64990 (2024-10/11), FAOP/67372 (2025-03-28),
  FAOP/70616 (2025-10-03). Source ``nse_circular``.

* **Stocks**: the per-symbol-day lot is reconstructed from the bhavcopy's own
  value/volume columns (legacy ``VAL_INLAKH`` and UDiFF ``TtlTrfVal`` are
  (strike+premium)-basis traded value; the ratio to ``vol*(strike+settle)``
  is the lot size to within ~1%, after excluding the expiring series on its
  expiry day). Plateaus are detected and validated against (a) the official
  quarterly revision circular dates and (b) the UDiFF ``NewBrdLotQty`` column
  (official per-contract lots, 2024-07-08+). Source ``derived`` or
  ``nse_bhavcopy`` when the official column pins the value.

Usage (on athena, repo root):

    uv run python scripts/build_lot_size_reference.py
    uv run python scripts/build_lot_size_reference.py --out /tmp/lot_sizes.csv

Output: ``data/reference/lot_sizes.csv`` (symbol, effective_from, lot_size,
source) plus a coverage/validation summary on stdout.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from mktmtm.config.settings import settings
from mktmtm.progress import track

DEFAULT_OUT = settings.reference_data_dir / "lot_sizes.csv"

UDIFF_NEED = ["TckrSymb", "XpryDt", "StrkPric", "SttlmPric", "TtlTradgVol", "TtlTrfVal", "NewBrdLotQty"]
LEGACY_NEED = ["SYMBOL", "EXPIRY_DT", "STRIKE_PR", "SETTLE_PR", "CONTRACTS", "VAL_INLAKH"]

WINDOW_START = "2021-08-02"

# ---------------------------------------------------------------------------
# Official index schedule (from NSE circulars; see module docstring for refs).
# effective_from = first trade date the revised lot applies to new contracts.
# ---------------------------------------------------------------------------
INDEX_SCHEDULE: dict[str, list[tuple[str, int]]] = {
    "NIFTY": [
        ("2021-04-30", 50),   # FAOP/47854 (75 -> 50), pre-window
        ("2024-04-26", 25),   # FAOP/61415
        ("2024-11-21", 75),   # FAOP/64625 (+64672/64990); 2024-11-20 was a holiday
        ("2025-10-28", 65),   # FAOP/70616
    ],
    "BANKNIFTY": [
        ("2021-01-01", 25),   # unchanged by FAOP/47854; 20->25 predates window
        ("2023-04-28", 15),   # FAOP/56233
        ("2024-11-21", 30),   # FAOP/64625
        ("2025-04-25", 35),   # FAOP/67372
        ("2025-10-28", 30),   # FAOP/70616
    ],
    "FINNIFTY": [
        ("2021-01-01", 40),   # unchanged by FAOP/47854
        ("2024-04-26", 25),   # FAOP/61415
        ("2024-11-21", 65),   # FAOP/64625
        ("2025-10-28", 60),   # FAOP/70616
    ],
    "MIDCPNIFTY": [
        ("2022-01-24", 75),   # launch lot (first data day)
        ("2024-04-26", 50),   # FAOP/61415
        ("2024-11-21", 120),  # FAOP/64625
        ("2025-04-25", 140),  # FAOP/67372
        ("2025-10-28", 120),  # FAOP/70616
    ],
    "NIFTYNXT50": [
        ("2024-04-24", 10),   # launch lot (FAOP/64625 lists existing 10)
        ("2024-11-21", 25),   # FAOP/64625
    ],
    "NIFTYFPI": [],           # launched 2025-06-11; lot taken from bhavcopy
}

# Official stock-revision circular announce dates. Effective dates land ~1
# month later (far-month phase-in); a derived step must fall inside
# [announce+15, announce+60] days to count as a circular-matched change.
STOCK_REVISION_ANNOUNCE: list[date] = [
    date(2021, 9, 30),   # FAOP/49795
    date(2022, 3, 31),   # FAOP/51841
    date(2022, 9, 30),   # FAOP/53920
    date(2023, 3, 31),   # FAOP/56230
    date(2023, 9, 29),   # FAOP/58700
    date(2024, 3, 28),   # FAOP/61369
    date(2024, 9, 30),   # FAOP/64286
    date(2025, 3, 28),   # FAOP/67360
    date(2025, 9, 30),   # FAOP/70554
    date(2026, 3, 30),   # FAOP/73549
]

# Step-detection tuning -----------------------------------------------------
_STEP_REL_TOL = 0.06        # deviation from plateau median that starts a step
_STEP_PERSIST_DAYS = 10     # deviation must persist this many valid days
_MIN_PLATEAU_DAYS = 15      # plateaus shorter than this merge into neighbours
_TRANSITION_DAYS = 35       # calendar days after a step treated as phase-in


def parse_bhavcopy_date(fname: str) -> str:
    stem = fname[:-4] if fname.lower().endswith(".csv") else fname
    stem = stem[8:] if stem.startswith("fo_bhav_") else (stem[2:] if stem.startswith("fo") else stem)
    if stem.endswith("bhav"):
        stem = stem[:-4]
    for fmt in ("%d%b%Y", "%Y%m%d"):
        try:
            return pd.to_datetime(stem, format=fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def aggregate_bhavcopy(bhavcopy_dir: Path) -> pd.DataFrame:
    """Per (symbol, day): value/volume evidence + official UDiFF lot mode."""
    rows = []
    files = sorted(bhavcopy_dir.glob("*.csv"))
    for fp in track(files, "Parsing bhavcopy", unit="files", label=lambda p: p.name):
        d = parse_bhavcopy_date(fp.name)
        if not d:
            continue
        try:
            df = pd.read_csv(fp, usecols=lambda c: c in UDIFF_NEED or c in LEGACY_NEED)
        except Exception:
            continue
        if "TckrSymb" in df.columns and "NewBrdLotQty" in df.columns:
            df = df[df["TtlTradgVol"].fillna(0) > 0]
            if not len(df):
                continue
            df["exp"] = pd.to_datetime(df["XpryDt"], errors="coerce")
            df["sym"] = df["TckrSymb"]
            df["px"] = df["StrkPric"].fillna(0) + df["SttlmPric"].fillna(0)
            df["not_exp"] = df["exp"] > df.groupby("sym")["exp"].transform("min")
            g = df.assign(date=d).groupby(["date", "sym"]).agg(
                official=("NewBrdLotQty", lambda s: float(s[s > 0].mode().iloc[0]) if (s > 0).any() else np.nan),
                official_uniq=("NewBrdLotQty", lambda s: int(s[s > 0].nunique())),
                val_x=("TtlTrfVal", lambda s, df=df: float(s[df.loc[s.index, "not_exp"]].sum())),
                volpx_x=("TtlTradgVol", lambda s, df=df: float((s * df.loc[s.index, "px"] * df.loc[s.index, "not_exp"]).sum())),
                vol=("TtlTradgVol", "sum"),
            ).reset_index()
            g["era"] = "udiff"
            rows.append(g)
        elif "SYMBOL" in df.columns and "VAL_INLAKH" in df.columns:
            df = df[df["CONTRACTS"].fillna(0) > 0]
            if not len(df):
                continue
            df["exp"] = pd.to_datetime(df["EXPIRY_DT"], format="%d-%b-%Y", errors="coerce")
            df["sym"] = df["SYMBOL"]
            df["px"] = df["STRIKE_PR"].fillna(0) + df["SETTLE_PR"].fillna(0)
            df["not_exp"] = df["exp"] > df.groupby("sym")["exp"].transform("min")
            g = df.assign(date=d).groupby(["date", "sym"]).agg(
                official=("VAL_INLAKH", lambda s: np.nan),
                official_uniq=("VAL_INLAKH", lambda s: 0),
                val_x=("VAL_INLAKH", lambda s, df=df: float(s[df.loc[s.index, "not_exp"]].sum())),
                volpx_x=("CONTRACTS", lambda s, df=df: float((s * df.loc[s.index, "px"] * df.loc[s.index, "not_exp"]).sum())),
                vol=("CONTRACTS", "sum"),
            ).reset_index()
            g["era"] = "legacy"
            rows.append(g)
    out = pd.concat(rows, ignore_index=True)
    out["derived"] = np.where(
        out["era"] == "udiff",
        out["val_x"] / out["volpx_x"].replace(0, np.nan),
        out["val_x"] * 1e5 / out["volpx_x"].replace(0, np.nan),
    )
    # Signal used for plateau detection: derived value where available, else
    # the official UDiFF lot (keeps thin symbols with no volume signal covered).
    out["signal"] = out["derived"].where(out["derived"].notna(), out["official"])
    return out


def detect_plateaus(days: pd.DataFrame) -> list[tuple[date, float]]:
    """Change-point detection over one symbol's signal series."""
    if "signal" not in days.columns:
        days = days.assign(signal=days["derived"])
    pts = [(pd.to_datetime(r["date"]).date(), float(r["signal"])) for _, r in days.iterrows()
           if not pd.isna(r["signal"])]
    if not pts:
        return []
    plateaus: list[tuple[date, list[float]]] = [(pts[0][0], [])]
    for day, val in pts:
        _, cur_vals = plateaus[-1]
        med = float(np.median(cur_vals)) if cur_vals else val
        if med == 0:
            med = 1.0
        if abs(val - med) / med > _STEP_REL_TOL:
            future = [v for d, v in pts if d > day][:_STEP_PERSIST_DAYS]
            if len(future) >= max(3, _STEP_PERSIST_DAYS // 2) and \
               abs(float(np.median(future)) - med) / med > _STEP_REL_TOL:
                plateaus.append((day, []))
        plateaus[-1][1].append(val)
    return [(start, float(np.median(vals))) for start, vals in plateaus]


def merge_short_plateaus(steps: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """Drop plateaus shorter than ``_MIN_PLATEAU_DAYS`` and collapse
    adjacent near-identical values (the longer plateau's value wins)."""
    if len(steps) <= 1:
        return steps
    merged: list[tuple[date, float]] = []
    for i, (start, val) in enumerate(steps):
        nxt = steps[i + 1][0] if i + 1 < len(steps) else date(2030, 1, 1)
        if (nxt - start).days < _MIN_PLATEAU_DAYS and merged:
            continue  # short plateau: keep the previous (longer) plateau
        merged.append((start, val))
    out: list[tuple[date, float]] = []
    for start, val in merged:
        if out and abs(out[-1][1] - val) / max(1.0, val) < 0.02:
            continue
        out.append((start, val))
    return out


def build_stock_schedule(days: pd.DataFrame) -> list[tuple[str, str, int, str]]:
    """Per-symbol change-point rows for the stock schedule.

    Runs on the full symbol series (both bhavcopy eras). Plateau value: the
    official UDiFF lot where the plateau has unambiguous official days
    (``official_uniq == 1``), else the rounded median of the derived signal.
    """
    rows: list[tuple[str, str, int, str]] = []
    for sym, g in days.groupby("sym"):
        g = g.sort_values("date")
        steps = merge_short_plateaus(detect_plateaus(g))
        prev_lot: int | None = None
        for idx, (start, med) in enumerate(steps):
            lot = int(round(med))
            if lot <= 0 or (prev_lot is not None and lot == prev_lot):
                continue
            source = "derived"
            nxt = steps[idx + 1][0] if idx + 1 < len(steps) else None
            seg = g[g["date"] >= start.isoformat()]
            if nxt is not None:
                seg = seg[seg["date"] < nxt.isoformat()]
            uniq1 = seg[seg["official_uniq"] == 1].dropna(subset=["official"])
            if len(uniq1) >= 5:
                # NSE's own per-contract lot, unambiguous on these days; it is
                # exact, so any deviation from the rounded derived value means
                # the derived rounding was off (systematic value-vs-settle bias).
                lot = int(round(float(uniq1["official"].median())))
                source = "nse_bhavcopy"
            rows.append((sym, start.isoformat(), lot, source))
            prev_lot = lot
    return rows


def matched_window(step_date: date) -> bool:
    """True when a step date falls in a known stock-revision effective window.

    Effective dates land ~1 month after the circular (far-month phase-in), and
    the volume-weighted signal crosses a few weeks into the phase-in — so the
    window spans announce+15 .. announce+75 days.
    """
    for ann in STOCK_REVISION_ANNOUNCE:
        lo = ann + timedelta(days=15)
        hi = ann + timedelta(days=75)
        if lo <= step_date <= hi:
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Build data/reference/lot_sizes.csv")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="Output CSV path")
    ap.add_argument("--bhavcopy-dir", default=str(settings.raw_data_dir / "bhavcopy" / "extracted"),
                    help="Directory of raw bhavcopy CSVs")
    args = ap.parse_args()

    days = aggregate_bhavcopy(Path(args.bhavcopy_dir))
    print(f"aggregated {len(days)} symbol-days "
          f"({days['date'].min()} .. {days['date'].max()})")

    # ---- index schedule (official circulars) ----
    rows: list[tuple[str, str, int, str]] = []
    for sym, pts in INDEX_SCHEDULE.items():
        if not pts:
            continue
        in_effect = None
        for eff, lot in pts:
            if eff <= WINDOW_START:
                in_effect = lot
            else:
                break
        if in_effect is not None:
            rows.append((sym, WINDOW_START, in_effect, "nse_circular"))
        for eff, lot in pts:
            if eff > WINDOW_START:
                rows.append((sym, eff, lot, "nse_circular"))

    # ---- stock schedule (derived, validated against official data) ----
    # Index symbols have an official circular schedule; exclude them from the
    # derived build so phase-in noise cannot add spurious rows. The full
    # (both-era) series is passed as one unit so plateaus span the UDiFF
    # boundary and NSE's official lot column anchors their values.
    index_syms = set(INDEX_SCHEDULE)
    stock_days = days[~days["sym"].isin(index_syms)].copy()
    rows.extend(build_stock_schedule(stock_days))

    # ---- symbols not covered above (e.g. NIFTYFPI): first plateau from data ----
    have = {r[0] for r in rows}
    for sym in sorted(days["sym"].unique()):
        if sym in have:
            continue
        sub = days[days["sym"] == sym].sort_values("date")
        steps = merge_short_plateaus(detect_plateaus(sub))
        if steps:
            rows.append((sym, steps[0][0].isoformat(), int(round(steps[0][1])), "derived"))

    # ---- assemble, dedupe consecutive identical lots, write ----
    df = pd.DataFrame(rows, columns=["symbol", "effective_from", "lot_size", "source"])
    df = df.sort_values(["symbol", "effective_from"])
    dedup: list[tuple[str, str, int, str]] = []
    for sym, g in df.groupby("symbol"):
        for _, r in g.iterrows():
            if dedup and dedup[-1][0] == sym and dedup[-1][2] == int(r["lot_size"]):
                continue
            dedup.append((sym, r["effective_from"], int(r["lot_size"]), r["source"]))
    df = pd.DataFrame(dedup, columns=["symbol", "effective_from", "lot_size", "source"])
    # Backfill each symbol's first row to its first data day, so coverage
    # starts at the first day the symbol appears in the bhavcopy.
    first_data = days.groupby("sym")["date"].min()
    first_rows = df.groupby("symbol").first()
    for sym, row in first_rows.iterrows():
        if sym in first_data and row["effective_from"] > first_data[sym]:
            df.loc[(df["symbol"] == sym) & (df["effective_from"] == row["effective_from"]),
                   "effective_from"] = first_data[sym]
    df = df.sort_values(["symbol", "effective_from"])
    df.to_csv(args.out, index=False)
    print(f"wrote {len(df)} schedule rows -> {args.out}")

    # ---- coverage + validation vs the dataset's own symbol-days ----
    sched: dict[str, list[tuple[date, int]]] = {}
    for sym, g in df.groupby("symbol"):
        sched[sym] = [(datetime.strptime(e, "%Y-%m-%d").date(), int(lv))
                      for _, e, lv, _ in g.itertuples(index=False)]

    n_total = n_covered = n_derived = n_match = n_match_notrans = n_in_trans = 0
    n_steps_total = n_steps_matched = 0
    unmatched_steps: list[tuple[str, str, int]] = []
    for sym, g in days.groupby("sym"):
        pts = sched.get(sym, [])
        for _, r in g.iterrows():
            d = datetime.strptime(r["date"], "%Y-%m-%d").date()
            lot = None
            for eff, lv in pts:
                if eff <= d:
                    lot = lv
                else:
                    break
            n_total += 1
            if lot is not None:
                n_covered += 1
            if pd.isna(r["derived"]):
                continue
            n_derived += 1
            if lot is not None and lot > 0:
                rel = abs(float(r["derived"]) - lot) / lot
                if rel <= 0.03:
                    n_match += 1
                in_trans = any(eff <= d <= eff + timedelta(days=_TRANSITION_DAYS) for eff, _ in pts)
                if in_trans:
                    n_in_trans += 1
                elif rel <= 0.03:
                    n_match_notrans += 1
        # step-date validation: every schedule step after the first must land
        # in a known revision window, except for index symbols (circulars)
        for idx in range(1, len(pts)):
            eff = pts[idx][0]
            n_steps_total += 1
            if sym in INDEX_SCHEDULE or matched_window(eff):
                n_steps_matched += 1
            else:
                unmatched_steps.append((sym, eff.isoformat(), pts[idx][1]))

    print(f"\ncoverage: {n_covered}/{n_total} symbol-days "
          f"({n_covered / n_total * 100:.2f}%) have a schedule lot")
    if n_derived:
        print(f"derived lot within 3% of schedule (all days with signal): "
              f"{n_match}/{n_derived} ({n_match / n_derived * 100:.2f}%)")
    dn = n_derived - n_in_trans
    if dn:
        print(f"derived lot within 3% of schedule (excluding {_TRANSITION_DAYS}-day "
              f"phase-in windows after each step): {n_match_notrans}/{dn} "
              f"({n_match_notrans / dn * 100:.2f}%)")
    print(f"schedule steps: {n_steps_total}, matched to a known revision window: "
          f"{n_steps_matched} ({n_steps_matched / n_steps_total * 100:.1f}%)")
    if unmatched_steps:
        print(f"unmatched steps (not near any quarterly revision circular; "
              f"typically corporate-action adjustments or symbol launches): "
              f"{len(unmatched_steps)}")
        for sym, eff, lot in sorted(unmatched_steps)[:30]:
            print(f"   {sym:12s} {eff}  lot={lot}")
    print(f"\nsymbols: {len(df['symbol'].unique())}; rows: {len(df)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
