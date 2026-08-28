# MktMtm — Understanding Our Data (Plain-English Report for the ML Phase)

> **Audience:** someone who knows machine learning but is new to options/derivatives.
> **Purpose:** explain, from zero, every piece of data we have, what it's telling us, which
> variables we'll feed the models, and what extra signal we can squeeze out.
> **Verified against:** live `options_db` queries + pipeline source (`src/mktmtm/`), 2026-08-19.
> **Companion docs:** `docs/data/database-report.md` (schema), `docs/reference/nse_options_data_calculation_formulae.pdf` (formulas).

---

## 1. What we are trying to build

Two supervised models that, given everything known about a stock/index **at the end of
trading day T**, predict what happens **on day T+1 (tomorrow)**:

| Model | Predicts | Type |
|---|---|---|
| Direction model | Will tomorrow be **bullish (up)** or **bearish (down)**? | Classification (label 1/0) |
| Magnitude model | **By how much** will the price move? | Regression (a number, e.g. +0.8%) |

Both models are trained on the same input data; they just have different targets (labels).
Everything in this report is about understanding that input data and where the targets come from.

---

## 2. Options 101 — the bare minimum

An **option** is a contract that gives the buyer the *right* (not the obligation) to buy or
sell something at a fixed price, on or before a fixed date. That "something" is called the
**underlying** — for us it's an NSE stock or index (NIFTY, TATASTEEL, …).

Think of it like an insurance policy:

- **Premium** = the price you pay for the option (like an insurance premium).
- **Strike price** = the agreed price at which you can buy/sell (like the insured amount).
- **Expiry** = the date the contract ends (like the policy end date).
- **CE (Call)** = the right to *buy* at the strike. You buy a call when you think the price goes **up**.
- **PE (Put)** = the right to *sell* at the strike. You buy a put when you think the price goes **down**.

Every option contract is identified by: **symbol + expiry + strike + type (CE/PE)**.
On any given day, NIFTY alone has ~1,700 such contracts trading — that's the "huge JSON" you saw.

**Why do options tell us about tomorrow?** The price people pay for options is not random.
It encodes what the market believes about the future — how big the moves will be, which
direction is feared, how nervous traders are. That belief is measurable. This is exactly the
signal we want our model to learn.

---

## 3. Where our data comes from (4 sources)

| Source | What it gives us | Frequency |
|---|---|---|
| **NSE F&O bhavcopy** | The official end-of-day file listing **every traded option contract**: symbol, expiry, strike, CE/PE, open/high/low/close, settle price, volume, (and open interest — see §11). Downloaded since 2020-04-13. | Daily |
| **Yahoo Finance** | **Spot price** of each underlying (the stock/index's own closing price — the thing we're predicting). | Daily |
| **MIFOR** (rate file) | The interest rate used in the Black–Scholes formula (Mumbai Interbank Forward Offer Rate). A technical input, not a signal. | Daily |
| **Earnings calendar** | Upcoming corporate earnings dates — earnings are scheduled volatility bombs; knowing one is coming matters. | Quarterly-ish |

---

## 4. What's in the database

One table matters for training: `option_metrics`. **One row = one symbol on one trading day**
(primary key `(symbol, date)`). Live counts (checked 2026-08-19):

- **186,234 rows** · **222 symbols** (5 indices + 217 stocks) · **2020-04-13 → 2026-08-14** (~6.4 years)
- ~1,198 trading days per symbol — i.e. each symbol's price history is ~1,200 days long

So the training matrix is: ~186,000 rows × (a few dozen feature columns). That's a *small*
dataset by ML standards — no memory or compute problem. The challenge is signal, not size.

A real row (NIFTY, 2026-08-14) looks like this:

```
symbol            = NIFTY
date              = 2026-08-14
underlying_price  = ~24,900        (NIFTY spot close that day)
interest_rate     = MIFOR rate %
strike_price      = ATM strike     (reference strike of the day)
expiry_30d/60d/90d= the ~30/60/90-day option expiries
upcoming_earning_date = (NIFTY: none; stocks: next earnings date)
rv_yz             = Yang–Zhang realized volatility (see §5)
ce  = { ivp: 60.0, ivr: 57.7, open: 221.05, high: 237.3, low: 175.65, close: 218.1,
        iv_30: 10.64, iv_60: 10.34, iv_90: 15.70,
        greeks: { delta: 0.568, gamma: 0.0009, theta: -10.94, vega: 16.63, rho: 4.11 },
        volume: 37,457,795, last_price_30d: 218.1 }
pe  = { same shape, put side }
option_chain = [ 1,718 elements — every NIFTY option contract that day ]
extras = {}
```

---

## 5. Every variable, explained

### 5.1 The scalar columns

| Column | What it is | What it tells us | Why it helps |
|---|---|---|---|
| `underlying_price` | The spot close (Yahoo) of the stock/index | Where the price ended today | The raw thing we predict tomorrow; also the base for all returns |
| `interest_rate` | MIFOR rate, used in Black–Scholes | Cost of money | Technical input; barely matters for daily moves |
| `strike_price` | Reference (ATM) strike of the day | Which strike is "at the money" | Defines which contracts are the liquid ones |
| `expiry_30d/60d/90d` | Dates of the ~30/60/90-day expiries | How far out each contract is | We convert these to "days to expiry" — contracts behave differently near expiry |
| `upcoming_earning_date` | Next earnings date, if within horizon | When a scheduled jump is coming | Earnings = expected vol spike; big predictor |
| `rv_yz` | **Realized volatility** (Yang–Zhang): the actual annualized volatility of the price over the recent window, computed from open/high/low/close (this captures overnight gaps too) | *What actually happened* — how wild the price really was | The ground truth against which we judge option prices (see IV–RV spread, §8) |

### 5.2 The `ce` and `pe` JSON blocks (call side and put side)

These two blocks hold analytics for the **~30-day at-the-money (ATM) contract** — the most
liquid, most information-rich option of the day. `ce` = call side, `pe` = put side, same keys.

| Key | What it is | What it tells us | Why it helps |
|---|---|---|---|
| `iv_30`, `iv_60`, `iv_90` | **Implied volatility** of the 30/60/90-day ATM option — the volatility number the market is *pricing in* (solved backwards from the premium via Black–Scholes) | **The market's own forecast of future price swings** | This is our single richest input: it aggregates every trader's opinion into one number |
| `ivp` | **IV percentile** (0–100): where today's IV sits vs the trailing window of IVs (e.g. 60 = higher than 60% of recent days) | Volatility *regime*: is the market currently cheap or expensive vs its own history? | Regime shifts predict future returns (low-IV periods historically precede calm, etc.) |
| `ivr` | **IV rank** (0–100): min–max version of the same idea | Same as ivp, different normalization | Redundant-ish with ivp but both are standard, cheap features |
| `open/high/low/close` | OHLC of the *option premium itself* (price of the call/put contract) | How the option traded that day | Premium levels and ranges are raw market data |
| `last_price_30d` | Premium of the 30d ATM contract | "How much does the market charge for a 30-day bet?" | Direct dollar view of option pricing |
| `volume` | Contracts traded in that option | Participation/liquidity | High volume = trustworthy prices; also interest level |
| `greeks` (nested) | The option's sensitivity measures — see below | How the option price responds to market changes | Reads of positioning and expected behavior |

### 5.3 The Greeks (nested in `ce`/`pe`)

The Greeks describe *how the option premium would change* when something else changes. For
the ATM contract they act as a compact fingerprint of the market's stance:

| Greek | What it is | Plain reading |
|---|---|---|
| `delta` | Change in premium per ₹1 move in the underlying | Call `0.568` ≈ the ATM call moves ₹0.57 when NIFTY moves ₹1. Also ≈ probability the option finishes in-the-money. **Puts are negative** (they gain when price falls). |
| `gamma` | How fast *delta* changes as the price moves | High gamma = the option's sensitivity flips fast (typical near expiry/ATM). Measures "pivot risk". |
| `theta` | Daily time decay of the premium (₹ per day) | `-10.94` = the option loses ₹10.94 of value every day it sits. The clock is always ticking. |
| `vega` | Change in premium per 1% change in IV | How much the option price reacts to volatility fear. |
| `rho` | Change in premium per 1% change in interest rate | Almost always small — the "boring" Greek. |

**What the combination tells us:** put `delta` vs call `delta`, put `iv` vs call `iv`, and
put premium vs call premium all reflect *fear*. If puts are pricier than calls, the market
is paying up for downside protection — that's bearish positioning. This asymmetry is signal.

### 5.4 `option_chain` — the giant JSON array

This is the "huge JSON with multiple variables" you asked about. It is a snapshot of
**every option contract of that symbol traded that day** — all strikes × all expiries × both
CE and PE. For NIFTY: **1,718 elements**. Each element:

```json
{ "expiry": "27-Oct-2026", "strike": 25550.0, "type": "PE",
  "settle": 1233.16, "open": 0.0, "high": 0.0, "low": 0.0, "close": 901.9,
  "volume": 0, "iv": 17.50, "delta": -0.6417 }
```

- `strike`, `type`, `expiry` — which contract this is
- `open/high/low/close`, `settle`, `volume` — how it traded (settle = NSE's official
  settlement price, the weighted average of the day's trades)
- `iv` and `delta` — **computed by our own pipeline** (bisection + Black–Scholes, see
  `process.py::_build_chain_rows`) for every single contract

**Why we will NOT feed this raw to a model:** 1,718 elements × 12 fields = ~20,000 numbers
per row, × 186,000 rows = ~3.7 billion values, mostly zero-volume contracts and far-OTM
noise. Models choke on that. The information lives in the *shape of the whole surface* —
which strikes are expensive, how skewed puts vs calls are, where volume concentrates —
so we **collapse the chain into a handful of scalar aggregates** (§8, Tier 3). The chain is
the raw material; the aggregates are the features.

### 5.5 `extras`

A reserved JSONB slot for future features. Currently always `{}`. Nothing to understand yet.

---

## 6. The labels — what "tomorrow" means

Supervised learning needs a target. We derive it from the data itself, per symbol:

1. Sort each symbol's rows by date.
2. `next_day_return = (underlying_price[tomorrow] / underlying_price[today]) − 1`
3. **Direction label:** `1` if `next_day_return > 0` else `0`
4. **Magnitude label:** `next_day_return` itself (the signed % move — the model learns size)

Important details:

- "Tomorrow" = the **next trading day** (the next row for that symbol), not the next calendar
  day — which is exactly what a real trade would see.
- The **last row of every symbol has no label** (no tomorrow yet) — dropped (~222 rows).
- Labels use only day T+1's close. Features use only info up to day T's close. As long as we
  respect that line, there's no leakage.

---

## 7. The features we'll train on

Three tiers. All are per-row scalars — the model never sees raw JSON.

### Tier 1 — Already scalar (lift out of JSONB)

| Feature | Source | What it tells the model |
|---|---|---|
| `rv_yz` | column | How volatile the price *actually* is right now |
| `ce.iv_30`, `pe.iv_30` | ce/pe | What the market *expects* for the next month |
| `ce.iv_60/90`, `pe.iv_60/90` | ce/pe | Expectations further out (see term structure, Tier 2) |
| `ce.ivp`, `ce.ivr`, `pe.ivp`, `pe.ivr` | ce/pe | Vol regime — cheap or expensive vs history |
| `ce.greeks.*`, `pe.greeks.*` | ce/pe | Positioning fingerprint (delta/gamma/theta/vega/rho) |
| `ce.close`, `pe.close`, `ce.volume` | ce/pe | Premium levels, participation |

### Tier 2 — Derived from existing columns

| Feature | How | What it tells the model |
|---|---|---|
| `ret_1d/5d/10d/20d` | % change of `underlying_price` over past N days (per symbol) | **Momentum** — short-term trend |
| `iv_rv_spread` | `iv_30 − rv_yz` | Is the market's forecast *more* or *less* nervous than reality? A wide gap usually closes — predicting *which way* the gap closes is predictive |
| `term_slope` | `iv_90 − iv_30` | Normal: near-term vol > far-term. Inverted (far > near) = stress. Slope flips signal regime changes |
| `put_call_iv_spread` | `pe.iv_30 − ce.iv_30` | **Fear gauge**: how much extra the market pays for puts |
| `days_to_expiry_30d` | `expiry_30d − date` | Contracts behave differently as expiry approaches |
| `days_to_earnings` | `upcoming_earning_date − date` | Earnings are scheduled jumps; proximity predicts vol and drift |

### Tier 3 — Aggregates squeezed out of the giant chain

| Feature | How (from the 1,718 elements) | What it tells the model |
|---|---|---|
| `atm_straddle` | CE+PE premium at the strike nearest spot, ~30d expiry | The market's own price tag on "any big move" — a direct forecast of magnitude |
| `skew_25d` | IV of 25-delta OTM put − IV of 25-delta OTM call | The classic **skew**: heavy OTM-put premium = traders hedging crashes (bearish), flat/inverted = complacency |
| `put_call_vol_ratio` | ΣPE volume / ΣCE volume | Which side is being traded — flow tells you where money is positioned |
| `otm_put_iv_premium` | IV of ~5%-OTM put − ATM IV | Tail-risk demand: how much extra people pay to insure a crash |
| `chain_volume` | Σ contracts across all strikes/expiries | Market participation — rising activity often precedes bigger moves |
| `chain_breadth` | % of strikes with nonzero volume | How wide/thin the market is — thin markets move more |

**Design note:** Tier 3 is where the "huge JSON" earns its keep. The aggregation turns ~20,000
raw numbers per row into ~6 informative ones. This is standard practice — options research
lives on the *surface shape*, not the raw grid.

---

## 8. Extra data we can derive that would help

### 8.1 Free — already in the files we pull (just not yet kept)

- **Open interest (OI)** — the bhavcopy files (both legacy and UDiFF) contain `OPEN_INT` and
  `CHG_IN_OI` per contract. Our loader currently **drops these columns** (`bhavcopy.py` maps
  only prices + volume). OI is the *number of open contracts* — i.e. how much money is
  *committed* to a strike/expiry, not just traded today. OI + price move = who's winning.
  OI-based put-call ratio is one of the most used institutional signals. **Cost: a schema
  tweak + re-run of process/ETL. High value.**
- **Overnight gap** = today's open vs yesterday's close (both in `underlying_price` history).
  Gap size/continuation is a classic momentum signal.
- **Rolling stats** — 5/20-day mean and std of returns (cheap, adds robustness to momentum).

### 8.2 Cheap external additions (one-time fetch)

| Addition | What it is | Value |
|---|---|---|
| **India VIX** | NSE's official 30-day expected-volatility index (free daily series) | The whole market's fear meter; our IV30 is per-symbol, VIX is the macro backdrop |
| **Futures basis** | Futures price − spot (futures are in the bhavcopy too, currently filtered out) | Premium/discount of futures = institutional directional lean |

### 8.3 Calendar/context (free, engineered)

- Day of week, month — real seasonality exists in Indian markets
- Days to next F&O expiry — expiry-day behavior is measurably different
- Post-earnings flag (days since last earnings) — drift after earnings is documented

---

## 9. How the models will consume all this (the shape of training)

1. **Flatten** — build one table: rows = (symbol, date), columns = ~35 features + 2 labels.
   ~186K rows × ~37 columns ≈ 25 MB. Tiny.
2. **Scale** — standardize features; fit scalers on the train split only (never the whole
   dataset — that's leakage).
3. **Split chronologically** — train on earlier dates, validate on later ones. A random
   split would let the model peek at the future and lie about its performance. Walk-forward
   validation is the honest protocol for "predict tomorrow".
4. **Two models, same matrix** — a classifier (direction) and a regressor (magnitude).
   Start with gradient-boosted trees (LightGBM/XGBoost) — they handle tabular data with
   mixed scales better than neural nets here, and train in seconds.
5. **Upgrade path** — if we later want an LSTM/Transformer, the same per-day feature rows
   get stacked into sequences (symbol, last 20 days, 35 features). Features unchanged.

---

## 10. Glossary (quick reference)

| Term | Meaning in one line |
|---|---|
| Underlying | The stock/index the option is written on; its price is what we predict |
| CE / PE | Call (bet on up) / Put (bet on down) option |
| Strike | The fixed price in the contract |
| Expiry | When the contract ends |
| Premium | The price paid for the option |
| ATM / OTM | At-the-money (strike ≈ spot) / out-of-the-money (strike away from spot) |
| Settle price | NSE's official reference price for the contract that day |
| IV | Implied volatility — the market's priced-in forecast of future swings |
| RV | Realized volatility — what the swings actually were |
| IV percentile/rank | Where today's IV sits vs recent history |
| Greeks (delta/gamma/theta/vega/rho) | Sensitivities of the option price to underlying, acceleration, time, vol, rates |
| Skew | How much pricier OTM puts are than OTM calls (fear asymmetry) |
| Straddle | Call + put at the same strike — a bet on "any big move" |
| Put-call ratio | Puts traded/held vs calls — a positioning gauge |
| Bhavcopy | NSE's daily end-of-day file of all traded F&O contracts |
| OI (open interest) | Number of open (unclosed) contracts — committed money |
| Yang–Zhang | A realized-volatility estimator using O/H/L/C that captures overnight gaps |
| MIFOR | Interest rate used in the pricing model (technical input) |

---

*Report source of truth: live `options_db` queries + `src/mktmtm/` (bhavcopy.py, process.py, storage SQL). Formulas: `docs/reference/nse_options_data_calculation_formulae.pdf`. Next step when you're ready: turn §7 into a real feature-engineering module.*
