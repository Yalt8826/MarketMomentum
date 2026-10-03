# NSE archive survey — what is actually fetchable

**Surveyed 2026-10-03.** Every row below was requested live and its status
recorded. History was probed at six dates spanning the study window, not
assumed.

This exists because the project had generalised one failure into a rule. The
deals investigation concluded that *"NSE bulk/block deal history cannot be
fetched programmatically"* — true for that report, and it was read as a
statement about NSE. It is not. Several archive families serve the full
2021–2026 window to a plain `requests` session.

---

## 1. The access rule

The boundary is the **host and path**, not NSE:

| Path | Behaviour |
|---|---|
| `nsearchives.nseindia.com/...` static files | **Full history**, reliably, with a warmed cookie session |
| `www.nseindia.com/api/*` | Bot-blocked — HTTP 503 to any programmatic client even with a fully warmed session |

Bulk and block deals fail because they are served *only* through the API path
(`/api/historical/*-deals`). That is a property of that endpoint, not of the
archive.

**Warming the session** is required for both. Use
`mktmtm.data.participant_oi.warm_session()` — it GETs `www.nseindia.com` to
obtain cookies and raises if none are issued, so the failure surfaces before a
thousand wasted requests rather than after.

---

## 2. Availability

All verified `200` with a parseable body on 2026-09-04 unless noted.

### Derivatives

| File | Content |
|---|---|
| `content/nsccl/fao_participant_oi_DDMMYYYY.csv` | Participant-wise open interest — **already ingested** |
| `content/nsccl/fao_participant_vol_DDMMYYYY.csv` | Participant-wise **trading volume** — same four categories, same column layout |
| `archives/fo/sec_ban/fo_secban_DDMMYYYY.csv` | F&O ban list — **now ingested**, see §4 |
| `content/nsccl/mwpl_cli_DDMMYYYY.xls` | Market-wide position-limit utilisation |
| `content/fo/fii_stats_DD-Mon-YYYY.xls` | FII derivatives statistics |
| `content/fo/BhavCopy_NSE_FO_...csv.zip` | F&O bhavcopy — **already ingested** |

### Equities

| File | Content |
|---|---|
| `archives/equities/mto/MTO_DDMMYYYY.DAT` | **Security-wise delivery position** — 2,633 symbols, traded qty, deliverable qty, delivery % |
| `content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip` | Equity cash bhavcopy — 2,662 EQ rows |
| `content/CM_52_wk_High_low_DDMMYYYY.csv` | 52-week high/low, corporate-action adjusted |
| `content/equities/bulk.csv`, `block.csv` | Bulk/block deals — **latest trading day only** |

### Indices

| File | Content |
|---|---|
| `content/indices/ind_close_all_DDMMYYYY.csv` | **165 indices**: OHLC, close, volume, turnover, **P/E, P/B, Div Yield** |
| `content/indices/ind_nifty<sector>list.csv` | Sector constituents with `Company Name`, **`Industry`**, `Symbol`, `Series` |

Sector lists confirmed: Bank (14), IT (10), Pharma (20), Auto (15), FMCG (15),
Metal (15).

### Probed and not found

`content/fo/fo_secban_*.csv` and `content/nsccl/fo_secban_*.csv` (the ban list
lives under `archives/`, not `content/`), `content/nsccl/volt_*.csv`,
`content/nsccl/combineoi_*.csv`, `content/equities/shortselling_*.csv`.

---

## 3. History was verified, not assumed

This is the check that killed the deals label, so it was done first.

Four families probed at six dates:

| Family | 2021-08-02 | 2022-03-01 | 2023-01-03 | 2024-01-02 | 2025-04-01 | 2026-09-04 | 2026-09-18 |
|---|---|---|---|---|---|---|---|
| participant volume | OK | 404 | OK | OK | OK | OK | OK |
| delivery (MTO) | OK | 404 | OK | OK | OK | OK | OK |
| all index closes | OK | 404 | OK | OK | OK | OK | OK |
| MWPL client-wise | OK | 404 | OK | OK | OK | OK | OK |

The 404 is **uniform across all four families**, which is the signature of a
closed market rather than a missing report. Confirmed against our own data:
`option_metrics` has **0 rows on 2022-03-01** and 155 on each adjacent trading
day. It was Mahashivratri.

> **The check that matters:** a family that serves only the latest day (like
> `bulk.csv`) returns 200 for today and 404 for every historical date. A family
> with real history returns 200 for every *trading* date. Those are
> distinguishable only by probing several dates, including old ones.

---

## 4. The F&O ban list, and its failure semantics

Ingested as `mktmtm.data.secban` after it was found that the live screen had
selected a banned symbol (see
[`../trading/results/bugs-and-fixes.md`](../trading/results/bugs-and-fixes.md)).

**The file is published on every trading day**, including days when nothing is
banned. A zero-ban day returns `200` with `NIL` appended to the title line:

```
Securities in Ban For Trade Date 03-JAN-2023: NIL
```

Verified at 2023-01-03 and 2025-04-01. A populated day:

```
Securities in Ban For Trade Date 04-SEP-2026:
1,INOXWIND
2,KAYNES
3,LICHSGFIN
4,SAIL
```

So three cases must stay distinct, and conflating the last two is the trap:

```
200 + symbols  ->  those symbols are banned
200 + NIL      ->  authoritatively zero bans
anything else  ->  UNKNOWN — a closed market, or we are being blocked
```

`fetch_secban` therefore **raises** on any non-200, the holiday 404 included.
Returning an empty set there would read exactly like a clean day, which is the
silent-omission failure this project has already suffered twice. The parser also
validates the date in the title against the date requested, so a stale or
misrouted response cannot silently apply another day's bans.

---

## 5. What each source is worth to this project

Ranked by value, with the reasoning rather than just an ordering.

### 1. MTO delivery data — the largest opportunity

The project's central documented obstacle is that **no per-symbol institutional
label exists and none can be obtained within its means.** Every modelling stage
is a response to that single fact.

Delivery percentage is per-symbol, **daily**, five years deep, covering **213 of
our 222** catalog symbols, mean 58.3% (range 3.0–100.0). It separates
speculative churn — intraday positions that never settle — from genuine
ownership transfer.

It is **not** a *who* label; retail takes delivery too. But it is the most
direct free proxy for real accumulation that exists, at roughly **1,250
observations per symbol** against the shareholding label's **19**.

**The decisive experiment**, cheap and pre-registerable: does delivery-weighted
volume track the quarterly `mf_shares` change better than `basis_excess`'s
+0.1374? Not attempted before submission — opening a new research line four
weeks out risks a null with no time to contextualise it.

### 2. Sector constituents — un-inerts a built control

The screen's sector cap is implemented, unit-tested, and **inert in production**
because *"the catalog carries no sector field."* These files carry `Industry`
directly. The cap exists because **15 strangles across one sector is one trade
wearing fifteen hats**, and a short-vol book's risk is overwhelmingly
correlated.

### 3. Participant trading volume — doubles the only real label

The project holds participant *open interest* — a **stock** of positions. This
is **flow**: contracts traded. Volume ÷ OI is position turnover, which
distinguishes *"FII rolled the book"* from *"FII built new positions."*

Directly relevant to Stage 1's null: the expiry calendar dominated prediction of
next-day *OI change* (AUC 0.61–0.72 from five calendar features alone). Volume
is a different target and may not be as mechanically calendar-driven.

### 4. `ind_close_all` — closes a documented gap

`CLAUDE.md` states FINNIFTY and MIDCPNIFTY *"have no working Yahoo ticker, so
they have no spot source and are legitimately absent."* This file publishes both
(`Nifty Financial Services`, `Nifty Midcap Select`) along with 163 other
indices — and adds daily **P/E, P/B and dividend yield**, where the project
currently carries no fundamental data at all.

### 5. MWPL utilisation — a crowding measure

How close each symbol sits to its market-wide position limit. The leading
indicator of the ban in §4, and a direct measure of how crowded institutional
positioning in a name has become.

### 6. Equity cash bhavcopy — a second spot source

NSE's own unadjusted close for every equity, which is exactly the basis
`option_metrics.underlying_price` requires. Would remove a third-party
dependency that has silently lost tickers (see
[`catalog-drift.md`](catalog-drift.md)) — though note it does **not** rescue the
three renamed symbols, which are absent from it too.

The *adjusted* series would still need constructing from corporate actions, so
this is a partial replacement rather than a drop-in.

---

## 6. Reproducing the survey

The probe scripts were written to the scratchpad rather than the repository,
since they are one-off reconnaissance. The pattern:

```python
from mktmtm.data.participant_oi import warm_session
s = warm_session()
r = s.get("https://nsearchives.nseindia.com/content/nsccl/"
          f"fao_participant_vol_{day:%d%m%Y}.csv", timeout=25)
```

Two rules observed while surveying, both worth keeping:

- **Delay between requests** (~1s). The survey made a few hundred requests
  total; the archive is a public service and this is a research project.
- **Probe old dates, not just today.** A latest-day-only report is
  indistinguishable from a full archive if you only ever ask for today.
