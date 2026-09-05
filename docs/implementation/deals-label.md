# The per-symbol institutional label: NSE bulk and block deals

**Status:** ingestion built and working; **history not yet acquired** — see §2.
Built 2026-09-05.

## 1. Why

Every modelling stage so far was validated indirectly, because no per-symbol
institutional ground truth was thought to exist:

| Stage | Validated against | Outcome |
|---|---|---|
| 1 — market flow | Market-wide `participant_oi` | Real label, no symbol dimension |
| 2 — regime HMM | Nothing (unsupervised) | Nothing to check states against |
| 3 — attribution | Earnings calendar | Placebo beat it |
| 4 — flow momentum | Self-supervised | No label at all |

The two stages with a falsifiable external check both concluded *"this is not
institutional."* That is what happens when ~30 features of detection machinery
are never once tested against a known whale.

Bulk and block deals are the only public, per-symbol, per-date, **named**
record of institutional transactions in this market.

## 2. Acquisition is blocked programmatically

Measured 2026-09-05:

| Route | Result |
|---|---|
| `nsearchives.nseindia.com/content/equities/bulk.csv` | ✅ 200 — but **latest trading day only** (156 rows, all 2026-09-04) |
| `.../block.csv` | ✅ 200 — latest day only (2 rows) |
| `/api/historical/bulk-deals?from=..&to=..` | ❌ **HTTP 503** |
| `/api/historical/block-deals?from=..&to=..` | ❌ **HTTP 503** |
| `bulk_DDMMYYYY.csv` and two other dated patterns | ❌ 404 |
| BSE `api.bseindia.com/.../BulkDeals/w` | ❌ 200 but HTML, not JSON |

The 503 persists with a fully warmed session — cookies obtained, referring page
fetched and returning 200, browser `User-Agent`, `Sec-Fetch-*` and
`X-Requested-With` set. The body is NSE's bot-detection page. The same report
downloads fine from a browser, so this is bot detection, not access control.

**Therefore history must be downloaded by hand**, from
<https://www.nseindia.com/report-detail/display-bulk-and-block-deals> — pick a
date range, download the CSV, drop it in `data/raw/deals/`, and run
`uv run mktmtm fetch-deals`. Filenames containing `bulk` or `block` have their
kind inferred. `--latest` additionally builds the record forward from today.

## 3. A structural problem with bulk deals

On the one day measured, **none of the 42 symbols with deals were in the
217-symbol F&O universe** — 0 of 158 rows, ₹0 of ₹6,525 crore.

This is not bad luck, it is definitional. A **bulk** deal requires one client to
trade **more than 0.5% of a company's listed shares in a day**. For an F&O
large cap that is billions of rupees in a single name on a single day, which
almost never happens; for a microcap it is routine. The day's sample is
accordingly ABH Healthcare, Ahimsa Industries, Antelopus Selan Energy — exactly
the profile the threshold selects for.

**Bulk deals are structurally the wrong instrument for an F&O-universe whale
detector.** They select microcaps by construction.

**Block deals** are the right concept — a single trade above ₹10 crore, which
large caps do produce — but there were only **2 in the whole day**. Extrapolated
that is roughly 500 a year across all symbols, of which some fraction falls in
the F&O universe. Over five years that plausibly supports an **event study**
(a few hundred to ~1,000 events, ~4 per symbol) but **not** a per-symbol-day
classifier.

⚠️ One trading day is a weak basis for this estimate. The 0/42 overlap is
consistent with the structural argument but is not itself strong evidence.
Confirming it needs the manual download, and that is the first thing to check
once the data is in hand.

## 4. The label is one-sided

Recorded here because any evaluation must state it:

A disclosed deal implies a whale. **The absence of one does not imply no
whale** — institutions slice orders below the disclosure threshold precisely to
avoid appearing here. So **recall is measurable against this table and
precision is not**, and an apparent false positive may be a real institution
that stayed under the threshold.

It is also a **cash-market** record while the project's features are F&O. That
link is plausible — institutions hedge and position in derivatives around large
cash trades — but it is a hypothesis under test, not an assumption.

## 5. What is built

- `data/deals.py` — `normalize_deals` (pure), `load_deal_file`,
  `fetch_latest_deals`, `write_deals`, `run_fetch_deals`
- `storage/sql/init_deals.sql` — the `deals` table, wired into `init_db.sql`
- `mktmtm fetch-deals` with `--file` / `--dir` / `--latest`
- 11 unit tests, all offline

Verified end to end: 158 rows loaded for 2026-09-04 (156 bulk, 2 block,
42 symbols).

Parsing handles what NSE actually emits: header case and whitespace drift,
Indian thousands separators (`2,31,000`), explicit date formats rather than
inference (`%b` and `%B` are identical for May, which has broken a build here
before), and the same client appearing twice in a day through different brokers
— summed, since the primary key would otherwise let the UPSERT silently keep
whichever row arrived last.

## 6. Recommended next step

Given §3, **quarterly shareholding patterns are probably the better label for
this project**, and the reasoning is worth stating plainly:

| | Bulk/block deals | Quarterly shareholding |
|---|---|---|
| Covers F&O large caps | ✗ bulk excludes them by construction; block is sparse | ✅ every listed company files |
| Granularity | Daily | Quarterly |
| Observations | ~4 block events per F&O symbol over 5 years | ~20 per symbol (217 × 20 ≈ 4,300) |
| Measures | An event | **Net institutional position change** |
| Acquisition | Manual download; API blocked | Scrape, messier |

Shareholding measures exactly what the project claims to detect — the change in
FII/DII holding in a *specific symbol* — and it covers the large caps that bulk
deals structurally miss. It would also directly repair
[Stage 3's failed validation](model-04-attribution-bridge.md), replacing the
earnings instrument that the placebo beat.

The deals ingestion stays: block deals remain a genuine, if thin, event-study
label, and the daily fetch accumulates a record from here on at no cost.
