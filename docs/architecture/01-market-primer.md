# 1. Market Primer — Everything You Need Before Reading the Rest

[← Back to index](README.md) · Next: [Data sources →](02-data-sources.md)

This document assumes **no prior market knowledge**. Every term the rest of the
documentation uses is defined here, in the order you need it. If a later document uses a
word you do not recognise, it is defined somewhere on this page.

---

## 1. The two markets: spot and derivatives

When you buy a share of Reliance on the stock exchange, you own a piece of the company. That
is the **spot market** (also "cash market"). The price you pay is the **spot price**, usually
written **S**.

A **derivative** is a contract whose value is *derived from* that share price. You never own
the share; you own a contract that pays off depending on what the share does. India's
National Stock Exchange (**NSE**) runs a large derivatives market called **F&O** —
**F**utures and **O**ptions.

This project uses almost no spot-market data. Nearly everything comes from F&O, because
that is where large players leave the most legible footprints.

## 2. Futures

A **futures contract** is an agreement to buy or sell something at a fixed price on a fixed
future date. If you buy a Reliance future expiring in November at ₹1,300, you have agreed to
pay ₹1,300 per share in November, whatever the actual price turns out to be.

Two things matter for this project:

**Basis.** The futures price is usually *not* the same as the spot price. If the stock is at
₹1,290 and the November future is at ₹1,300, the **basis** is ₹10. This is not free money —
it mostly reflects **cost of carry**: buying the stock today costs you interest on the money
for a few months, so a future must trade slightly higher to compensate. The interesting part
is the *deviation* from that fair cost, which is where demand pressure shows up. See
[`fut_basis_excess`](05-derived-features.md#futures-basis-and-roll).

**Rolling.** Futures expire. A player who wants to keep a position has to close the expiring
contract and open the next month's — this is a **roll**. Whether positions get rolled forward
or simply closed tells you whether conviction is being carried or abandoned.

## 3. Options

An **option** is the *right, but not the obligation*, to buy or sell at a set price.

- A **call option (CE)** gives the right to **buy** at the strike price.
- A **put option (PE)** gives the right to **sell** at the strike price.

The fixed price is the **strike price** (**K**). The date it expires is the **expiry**. The
price you pay for the option itself is the **premium**.

### The two sides — and why this project cares so much

Every option has two sides, and they are not symmetric:

| | Buyer (holder) | Writer (seller) |
|---|---|---|
| Pays or receives | **Pays** the premium | **Receives** the premium |
| Best case | Unlimited gain (calls) | Keeps the premium, nothing more |
| Worst case | Loses the premium | Potentially very large loss |
| Who typically does this | Retail traders, speculators | Institutions, market makers |

This asymmetry is central. Writing options means taking on large, open-ended risk in exchange
for a small, certain fee. That is a professional's trade, and it requires capital and hedging
machinery that a retail trader does not have.

**Measured in this project's own data:** institutional desks were net *short* (writers of)
calls on **99.9%** of 1,258 days, and net *long* puts on **98.3%**. Retail sits on the other
side. This is why a feature that separates the two sides is measuring something real.

### Moneyness

Where the strike sits relative to the current price:

```
                    A CALL option, stock at ₹1,290

    strike ₹1,100        strike ₹1,290        strike ₹1,500
    ┌─────────────┐     ┌─────────────┐      ┌─────────────┐
    │  IN THE     │     │   AT THE    │      │  OUT OF THE │
    │   MONEY     │     │    MONEY    │      │    MONEY    │
    │   (ITM)     │     │    (ATM)    │      │    (OTM)    │
    │             │     │             │      │             │
    │ already     │     │ right at    │      │ worthless   │
    │ profitable  │     │ the price   │      │ unless the  │
    │ to exercise │     │             │      │ stock rises │
    └─────────────┘     └─────────────┘      └─────────────┘
         expensive          moderate              cheap
```

For puts it is mirrored: a put is in-the-money when the strike is *above* the current price.

**Why OTM options matter here:** they are cheap, so they give the most leverage per rupee.
A large directional bet very often shows up as unusual activity in a far out-of-the-money
strike — which is exactly what the
[outlier-strike detector](05-derived-features.md#the-outlier-strike-detector) looks for.

## 4. Open interest vs volume: the key distinction

These two numbers look similar and mean completely different things.

- **Volume** (`CONTRACTS` in the raw data) = how many contracts **changed hands today**.
- **Open interest** (`OPEN_INT`) = how many contracts are **currently open and unsettled**.

```
   Day 1:  A buys 10 contracts from B     →  volume 10,  open interest 10
   Day 2:  A sells those 10 to C          →  volume 10,  open interest 10  (position moved)
   Day 3:  C sells them back to B         →  volume 10,  open interest  0  (position closed)
```

Volume was 10 every day. Open interest tells you a real position existed on days 1–2 and was
gone by day 3.

**This is the whole game.** Volume can be the same contract traded back and forth all day by
high-frequency algorithms — noise. A *change in open interest* means somebody opened or
closed a real position and is carrying risk overnight. That is what a large player leaves
behind.

`CHG_IN_OI` — the day-over-day change in open interest — is therefore the single most
important raw field in this project.

> ⚠️ **A trap that cost this project a bug.** NSE's bhavcopy reports `OPEN_INT` in **shares,
> not contracts**. To get a contract count you *divide* by the lot size. This was verified by
> checking that `SUM(OPEN_INT / lot)` across all stock futures reproduces the
> participant-wise contract total to within 0.02%, where the undivided sum is off by a factor
> of about 2,158. See [the flow-features report](../implementation/flow-features.md#42-bhavcopy-open-interest-is-in-shares-not-contracts-silent-and-pre-existing).

## 5. Lot size

You cannot trade one share's worth of a derivative. Contracts come in fixed bundles called
**lots** — one Reliance futures contract covers 500 shares, one Infosys contract 400.

NSE revises lot sizes periodically (roughly quarterly, plus adjustments after corporate
actions). The 217 symbols here carry a **median of 3 lot revisions each, up to 8**. Any
conversion between contracts and shares therefore needs the lot size *in effect on that
date*, not a single constant per symbol.

## 6. Expiry

Indian F&O contracts expire monthly (indices also weekly for part of this period). At any
moment several expiries trade simultaneously — the **near month**, **next month**, and one
further out.

This matters more than it sounds. Near-month contracts are far more active than far-month
ones, so any comparison of activity has to be made **within an expiry**, never pooled across
them — otherwise front-month noise drowns out everything else. The outlier detector does
exactly this and has [a test enforcing it](05-derived-features.md#the-outlier-strike-detector).

The expiry cycle also creates strong calendar patterns. Anything that looks predictive must
be checked against a plain "what day of the expiry cycle is it" baseline, because that
information is known in advance and is therefore worthless as a discovery.

## 7. Implied volatility (IV)

**Volatility** is how much a price moves around. Two flavours:

- **Realised volatility** — how much it *actually* moved, measured from history. This
  project uses the *Yang-Zhang* estimator, which uses the open, high, low and close of each
  day rather than just closes, making it far more efficient.
- **Implied volatility (IV)** — how much the market *expects* it to move, backed out of
  option prices.

IV is the interesting one. An option's price depends on the strike, the time left, interest
rates, the current price — all observable — and volatility, which is not. So you run the
pricing formula **backwards**: given the price the option actually trades at, what volatility
would make the formula produce that price? That answer is the implied volatility.

There is no closed-form way to invert it, so it is solved numerically by **bisection**:
guess a volatility, price the option, compare, narrow the range, repeat.

> ⚠️ **When it does not converge, the answer is `NULL` — never a number.** Earlier versions of
> this project returned `0.0` or `500.0` for degenerate inputs, and those fake numbers
> propagated into every downstream volatility feature. See
> [the IV integrity audit](../data/iv-integrity-audit.md).

IV is also the single strongest predictor of near-term movement in this dataset — which is a
problem, because it means a new feature can look impressive while merely rediscovering IV.
Hence the project's standing rule: **every feature is judged on its *partial* correlation
after controlling for IV**, not its raw correlation.

## 8. The Greeks: how an option reacts

The Greeks measure an option's sensitivity to each thing that can change. They are what makes
dealer hedging *mechanical*, and therefore predictable.

| Greek | Answers | Why this project cares |
|---|---|---|
| **Delta** (Δ) | If the stock moves ₹1, how much does the option move? | It is exactly how many shares a dealer must hold to be hedged. |
| **Gamma** (Γ) | How fast does delta itself change? | **The key one.** High gamma means the hedge must be constantly adjusted — forced, repeated trading in the underlying. |
| **Theta** (Θ) | How much value decays per day? | The writer's income; the buyer's bleed. |
| **Vega** (ν) | Sensitivity to a change in volatility. | Ties option positioning to volatility expectations. |
| **Rho** (ρ) | Sensitivity to interest rates. | Minor here; included for completeness. |
| **Vanna** | How delta changes when *volatility* changes. | Second-order hedging pressure. |
| **Charm** | How delta changes as *time* passes. | Hedging that must happen even if nothing moves. |

### Why gamma is the centre of the theory

A dealer who has written options is **short gamma**. As the stock rises their hedge requires
buying more; as it falls, selling more. They are forced to trade *in the same direction as
the move* — which amplifies it.

A dealer who is **long gamma** does the opposite: sells into rallies, buys dips, damping
movement.

So the theory says: measure the net gamma dealers are holding across all strikes
(**gamma exposure**, or **GEX**), and you have predicted whether their hedging will amplify
or damp tomorrow's moves.

**This project tested that and it did not hold.** See
[the findings](../models/gex-dealer-positioning-findings.md). The mechanism is real; the
tradeable prediction is not, at least not in this market on this data.

## 9. The option chain

At any moment, for one stock, dozens of options exist — every strike, both calls and puts,
several expiries. The full grid is the **option chain**:

```
        RELIANCE — spot ₹1,290, expiry 27-Nov-2026

   CALLS (CE)                 STRIKE          PUTS (PE)
   OI      chg_oi   price       │        price   chg_oi      OI
   ─────────────────────────────┼──────────────────────────────
   45,200  +1,200   198.5     1,100       8.2     -300    12,400
   88,700  +4,500   112.3     1,200      22.1   +1,100    56,900
  156,300  +9,800    58.2     1,290      58.4   +2,400   134,700   ← at-the-money
  201,500 +24,300    22.7     1,400     121.9     -800    41,200
   67,800  +2,100     8.4     1,500     229.5     -150     9,600
                              ↑
                    an unusually large build here is
                    what the outlier detector flags
```

This project stores the **entire chain, every day, for every symbol** — about **49.5 million
individual entries** — as JSON inside the database. See
[the database doc](04-database.md#42-option_metrics-the-fact-table).

## 10. Who is actually trading

NSE publishes, once a day, how many contracts each *category* of participant holds. This is
the only public data that attaches a name to a position.

| Category | Who they are | Role here |
|---|---|---|
| **FII** | Foreign Institutional Investors — overseas funds | Institutional "smart money" |
| **DII** | Domestic Institutional Investors — Indian mutual funds, insurers | Institutional, usually slower-moving |
| **Pro** | Proprietary desks — brokers trading their own capital | **The dealer / market-maker proxy** |
| **Client** | Everyone else — retail and small traders | The other side of the institutional trade |
| **TOTAL** | All of the above | Must balance: every long is somebody's short |

**`Pro` is the crucial one.** These are the desks that write options and hedge them — the
"dealer" of the theory in §8. When this project needed to know which way dealers actually
lean, it measured the `Pro` book rather than assuming the textbook answer, and found the
textbook answer was backwards for this market.

**The critical limitation:** this data is **market-wide**. One row per category per day, with
no symbol breakdown. It tells you institutions bought calls today; it cannot tell you *which
stock*. Joined to a per-symbol table on date alone, it is the same value for every symbol and
therefore useless for telling symbols apart. Recovering the per-symbol version is the
central problem of the project.

---

## Quick reference

| Term | Meaning |
|---|---|
| **Spot / S** | Current price of the underlying stock |
| **Strike / K** | The fixed price written into an option contract |
| **CE / PE** | Call option / Put option |
| **Premium** | The price of the option itself |
| **OI** | Open interest — contracts currently open |
| **chg_oi** | Change in open interest since yesterday — *new positioning* |
| **Volume** | Contracts traded today (includes churn) |
| **Lot size** | Shares per contract; NSE revises it periodically |
| **Expiry / DTE** | When the contract dies / days remaining until then |
| **ITM / ATM / OTM** | In / at / out of the money |
| **IV** | Implied volatility — the market's expected movement |
| **RV** | Realised volatility — actual past movement |
| **Basis** | Futures price minus spot price |
| **Roll** | Moving a position from the expiring contract to the next |
| **Greeks** | Delta, gamma, theta, vega, rho (+ vanna, charm) |
| **GEX** | Gamma exposure — net gamma dealers are holding |
| **Bhavcopy** | NSE's official end-of-day file of every contract traded |

[← Back to index](README.md) · Next: [Data sources →](02-data-sources.md)
