# The symbol catalog has drifted — 177 of 213 live names

**Measured 2026-10-03** against the F&O bhavcopy for 2026-10-01.

The shipped catalog is a **fixed 2021-era universe plus five years of drift**.
That is a defensible research choice, but it was not documented as one, and the
health gate was attributing the gap to the wrong cause.

---

## 1. How this was found

`scripts/verify_db_health.py` reports:

```
✓ 217/222 catalog symbols present (5 absent, no Yahoo spot source:
  FINNIFTY, LTIM, MIDCPNIFTY, PEL, TATAMOTORS)
```

FINNIFTY and MIDCPNIFTY are indices with no Yahoo ticker — documented and true.
But **LTIM, PEL and TATAMOTORS are liquid large caps**, one of them a NIFTY 50
constituent. "No Yahoo spot source" does not describe a stock like that, so it
was checked.

| Check | LTIM / PEL / TATAMOTORS |
|---|---|
| Yahoo `<SYMBOL>.NS` | **404 Not Found** (control: `RELIANCE.NS` → 23 bars) |
| NSE **equity cash** bhavcopy, 2026-10-01 | **absent** (control: RELIANCE at ₹1,167.70) |
| NSE **F&O** bhavcopy, 2026-10-01 | **absent** |

They are absent from Yahoo *and* from NSE's own cash market *and* from the F&O
segment. **They no longer exist under those symbols.** They were renamed:

| Old | New | Present in F&O 2026-10-01 |
|---|---|---|
| `TATAMOTORS` | **`TMPV`** (demerger) | yes |
| `LTIM` | **`LTM`** (LTIMindtree) | yes |
| `PEL` | — (Piramal, delisted from F&O) | no |

So the cause is **symbol renaming**, not a missing price feed. The health gate's
message is wrong, and the five-symbol figure understates the real gap by a wide
margin.

---

## 2. The real number

Comparing the shipped catalog against what actually traded on 2026-10-01
(`OPTSTK` ∪ `FUTSTK`):

| | Count |
|---|---|
| Live F&O stock symbols | **213** |
| Catalog `individual_securities` | 217 |
| In catalog but **no longer trading** | **40** |
| Trading but **absent from the catalog** | **36** |
| **Actual overlap** | **177 — 83% of the live universe** |

### The 36 absent live symbols

```
360ONE, ADANIPOWER, AMBER, ANANDRATHI, ATHERENERG, BAJAJHLDNG, BDL,
BLUESTARCO, COCHINSHIP, ENRIN, FORCEMOT, FORTIS, GODFRYPHLP, GVT&D,
HYUNDAI, KAYNES, KFINTECH, LTM, MAHABANK, MANKIND, MAZDOCK, MOTILALOFS,
NAM-INDIA, PGEL, POWERINDIA, PREMIERENE, RADICO, RVNL, SAGILITY, SUZLON,
SWIGGY, TMPV, UJJIVANSFB, UNOMINDA, VMM, WAAREEENER
```

**They are not marginal.** By open interest on 2026-10-01:

| Symbol | OI rank | OI (shares) |
|---|---|---|
| **SUZLON** | **3 of 213** | 691,184,800 |
| **ADANIPOWER** | **13** | 278,181,550 |
| **TMPV** | **24** | 180,704,000 |
| SAGILITY | 34 | 135,744,000 |
| SWIGGY | 45 | 116,177,675 |
| VMM | 58 | 83,410,300 |
| RVNL | 64 | 67,786,950 |
| MAHABANK | 67 | 65,234,000 |

The dataset is missing the **third** and **thirteenth** largest F&O names by
open interest, plus six more inside the top 70.

### The 40 dead catalog symbols

```
AARTIIND, ABFRL, ACC, APOLLOTYRE, ATGL, BALKRISIND, BERGEPAINT, BSOFT,
CESC, CHAMBLFERT, CYIENT, DALBHARAT, DEEPAKNTR, ESCORTS, EXIDEIND,
GRANULES, HFCL, HINDCOPPER, HUDCO, IGL, IIFL, IRB, IRCTC, JSL, LTIM,
M&MFIN, MGL, MRF, NCC, PEL, POONAWALLA, RAMCOCEM, SJVN, SYNGENE,
TATACHEM, TATACOMM, TATAMOTORS, TATATECH, TITAGARH, TORNTPOWER
```

These are **still listed companies** — they have shareholding filings and
historical F&O data. They have simply been removed from the F&O segment.

---

## 3. The asymmetry that matters

> **The dead symbols are visible. The live ones are invisible.**

The 40 dead names surface in the screen's refusal table as exactly
`40 no data for this date` — accurate, if uninformative about the cause. They
are counted, reported, and auditable.

The 36 live names appear **nowhere**. Nothing refuses them, because nothing
knows they exist. There is no row in any taxonomy that says "SUZLON was not
considered."

This is the survivorship direction worth stating explicitly in any write-up:

- Names that **left** F&O are **retained** in the universe
- Names that **joined** F&O are **excluded** from it

A universe constructed in 2021 and never revised therefore drifts toward older,
established names and away from new entrants — and new F&O entrants are
systematically the higher-volatility, higher-retail-interest names.

---

## 4. Why the catalog is *not* being regenerated

`CLAUDE.md` states the catalog "must never regenerate," and that is the right
call **for the research**, for two reasons:

1. **It avoids look-ahead in universe selection.** Deriving the universe from
   what trades *today* means every historical row is conditioned on the symbol
   having survived to 2026 — a textbook survivorship bias, and a worse one than
   the drift it would fix.
2. **Comparability.** Every published result — the Stage 5 IC of +0.1374, the
   Stage 1 null, Gate 0's +4.56% over 7,706 trades — is measured on this
   universe. Re-deriving it invalidates the lot.

What *was* wrong is that this was never written down as a deliberate choice with
a measured cost. It now is, and the cost is: **177 of 213, 83% coverage, biased
toward established names.**

### Does it threaten the headline result?

Checked, and **no** — see
[`../implementation/pre-submission-audit.md` §8](../implementation/pre-submission-audit.md).
The Stage 5 time gradient was re-measured on a constant sub-universe with the
presence threshold swept from 50% to 100%, and it survives with 73% of its
magnitude retained. The full-sample IC is in fact *higher* on cleaner universes
(+0.1537 vs +0.1374 published).

---

## 5. The screen is a different case

A **trading tool** should trade what is tradeable today. The screen currently
cannot see SUZLON, ADANIPOWER or TMPV at all, which for a daily product is
simply wrong — the research argument for a frozen universe does not apply to a
tool making live decisions.

**This was deliberately not changed.** Gate 3 is mid-window: one eligible
record exists (2026-10-01) and five more are needed. Changing the screen's
universe between a gate's first and second records would mean the six months
were not generated by one consistent method, which is precisely the kind of
quiet change that makes a forward test worthless.

The right sequencing is: source the screen's universe from the live bhavcopy
**after** Gate 3 completes, or **before** its first record — not between.

---

## 6. Recommended wording for the health gate

The current message attributes the gap to a missing price source. Accurate
wording:

```
217/222 catalog symbols present.
  2 indices with no spot source (FINNIFTY, MIDCPNIFTY)
  3 renamed out of existence (TATAMOTORS->TMPV, LTIM->LTM, PEL delisted)
Universe is FIXED at the 2021 catalog by design: covers 177 of 213 live
F&O names (83%). 36 live symbols absent, 40 catalog symbols no longer
trading. See docs/data/catalog-drift.md.
```
