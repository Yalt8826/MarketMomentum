# 03 — Futures and Options, From Zero

[← Who trades](02-who-trades-and-whales.md) · [Index](README.md) · Next: [Open interest →](04-open-interest-the-footprint.md)

This is the longest background document, because everything the project measures
lives here. Take it slowly. Every term is defined before it is used.

---

## 1. The idea of a derivative

A **derivative** is a contract between two people whose value depends on
something else — here, a share price. Neither party owns the share. They own an
agreement.

Two kinds exist on NSE, and this project uses both:

- A **future** is an *obligation*. Both sides must go through with it.
- An **option** is a *right*. One side may walk away; the other may not.

That asymmetry is why options cost money up front and futures do not.

## 2. Futures

> **A futures contract is an agreement to buy or sell something at a fixed price
> on a fixed future date.**

If you buy the November Reliance future at ₹1,300, you have agreed to pay ₹1,300
per share in November, whatever the stock actually does. If it is at ₹1,400 by
then, you have made ₹100 a share. If it is at ₹1,200, you have lost ₹100 a
share, and you must pay.

Three things about futures matter for this project.

### 2.1 The basis, and why it is not free money

The future almost never trades at the same price as the stock. If the stock is
at ₹1,290 and the November future at ₹1,300, the **basis** is +₹10.

That gap is not an arbitrage opportunity. It is mostly **cost of carry**: if you
bought the actual share today, your money would be tied up for three months and
you would forgo the interest you could have earned on it. So the future has to
trade *higher* by roughly that interest to make the two routes equivalent.

```
        fair futures price  =  spot × (1 + interest rate × time)
                            −  any dividend expected before expiry
```

The interesting quantity is therefore not the basis but the **excess basis** —
how far the actual basis sits *above or below* the fair cost of carry:

```
   fut_basis_excess  =  (annualised basis %)  −  (interest rate %)
```

**A positive excess means somebody is paying up for leveraged long exposure.**
They want the position badly enough to accept a worse-than-fair price. That
"somebody" is exactly who this project is looking for — and `basis_excess` turns
out to be the single most useful number in the entire project (documents 10, 12
and 14).

### 2.2 Expiry and rolling

Futures die. Indian stock futures expire on the last Thursday of each month. A
participant who wants to keep a position past expiry has to close the dying
contract and open the next month's — a **roll**.

Whether positions are rolled or simply allowed to die tells you whether
conviction is being carried forward or abandoned. The project measures this as
`fut_roll_ratio`.

### 2.3 Leverage

You do not pay ₹1,300 a share to buy a future. You post a **margin** — typically
10–20% — and the exchange marks your position to market daily. This is why a
fund can get ₹500 crore of exposure with ₹75 crore of cash, and why futures are
the natural instrument for "I want the exposure now, I will buy the stock
later."

## 3. Options

> **An option is the right, but not the obligation, to buy or sell at a fixed
> price before a fixed date.**

- A **call option (CE)** gives the right to **buy** at the fixed price.
- A **put option (PE)** gives the right to **sell** at the fixed price.

The fixed price is the **strike price**, written **K**. The date is the
**expiry**. The price you pay for the option itself is the **premium**.

### 3.1 A worked example

Reliance is at ₹1,290. You buy a call with strike ₹1,400 expiring in a month,
for a premium of ₹22.

| What Reliance does by expiry | What your call is worth | Your profit |
|---|---|---|
| Falls to ₹1,200 | ₹0 — you would never buy at 1,400 | −₹22 (the premium) |
| Stays at ₹1,290 | ₹0 | −₹22 |
| Rises to ₹1,410 | ₹10 | −₹12 |
| Rises to ₹1,500 | ₹100 | +₹78 |
| Rises to ₹1,800 | ₹400 | +₹378 |

You risked ₹22 to control ₹1,290 worth of stock. That is the appeal, and it is
also the trap: **most options expire worthless**, and the ₹22 is gone in every
one of the first three rows.

### 3.2 The two sides — the most important table in this document

Every option has a buyer and a seller, and they are *not* mirror images in any
practical sense.

| | **Buyer** (holder) | **Writer** (seller) |
|---|---|---|
| Cash today | **Pays** the premium | **Receives** the premium |
| Best possible outcome | Very large gain | Keeps the premium, nothing more |
| Worst possible outcome | Loses the premium | Potentially unlimited loss |
| What they need | ₹22 | Capital, margin, and a hedging desk |
| Who actually does it | Retail, speculators | Institutions, market makers |
| Their view | "This will move a lot" | "This will not move much" |

Writing an option means accepting a large, open-ended risk in exchange for a
small, certain fee. It is an insurance business. It needs balance-sheet capital
and machinery to hedge with — which is exactly why, as document 02 showed, the
writers are institutional on 99.9% of days and the buyers are retail.

> **This asymmetry is why the project's "buildup quadrant" feature exists.** If
> you can tell whether a new position was *bought* or *written*, you know
> something about *who* opened it and *what they think*. Document 10 §3.

### 3.3 Moneyness — where the strike sits

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
       expensive           moderate              cheap
       ~₹200                 ~₹58                 ~₹8
```

For puts it mirrors: a put is in-the-money when the strike is *above* the
current price.

**Why out-of-the-money options matter here.** They are cheap, so they give the
most leverage per rupee, so a big directional bet very often appears as
implausible activity in one far-away strike. That is precisely what the
[outlier-strike detector](10-the-features.md#2-the-outlier-strike-detector)
looks for.

### 3.4 Lot size — you cannot buy one

Derivatives trade in fixed bundles called **lots**. One Reliance futures
contract covers 500 shares; one Infosys contract 400. You cannot trade a
fraction of a lot.

NSE revises lot sizes roughly quarterly and after corporate actions. The 217
symbols in this project carry a **median of 3 revisions each, up to 8** — so any
conversion between "contracts" and "shares" needs the lot size *in force on that
date*, not one constant per symbol. The project ships a
`data/reference/lot_sizes.csv` change-point schedule for exactly this.

### 3.5 Expiry, and the calendar problem

Indian F&O contracts expire monthly; indices also had weekly expiries for part of
the period. At any moment several expiries trade at once — the **near month**,
the **next month**, and one further out.

Near-month contracts are far more active than far-month ones. So **any
comparison of activity must be made within a single expiry**, never pooled
across them, or the front month drowns out everything else. The project's
outlier detector enforces this and has a test pinning it.

The expiry cycle also creates very strong, very predictable calendar patterns —
positions roll on a schedule published years in advance. This turns out to be a
serious problem for research:

> **A model that "predicts" institutional flow may simply have rediscovered the
> expiry calendar.**

Document 12's Stage 1 was defeated by exactly this. A five-feature calendar-only
baseline (days-to-expiry plus day-of-week) reaches an AUC of 0.61–0.72 on
next-day institutional flow, and **no feature set built in this project beats
it.**

## 4. The option chain

For one stock, on one day, dozens of options exist — every strike, both types,
several expiries. The whole grid is the **option chain**:

```
        RELIANCE — spot ₹1,290, expiry 27-Nov-2026

   CALLS (CE)                 STRIKE          PUTS (PE)
   OI      chg_oi   price       │        price   chg_oi      OI
   ─────────────────────────────┼──────────────────────────────
   45,200  +1,200   198.5     1,100       8.2     -300    12,400
   88,700  +4,500   112.3     1,200      22.1   +1,100    56,900
  156,300  +9,800    58.2     1,290      58.4   +2,400   134,700  ← at the money
  201,500 +24,300    22.7     1,400     121.9     -800    41,200
   67,800  +2,100     8.4     1,500     229.5     -150     9,600
                              ↑
                    an unusually large build here is
                    what the outlier detector flags
```

This project stores **the entire chain, every day, for every symbol** —
roughly **49.5 million individual entries** — inside PostgreSQL as JSON.
Document 09 explains how.

## 5. Vocabulary

| Term | Meaning |
|---|---|
| **Future** | Obligation to trade at a fixed price on a fixed date |
| **Basis** | Futures price minus spot price |
| **Cost of carry** | The interest component that makes a fair basis positive |
| **`basis_excess`** | Basis above fair carry — demand pressure. ⭐ the project's best feature |
| **Roll** | Moving a position from the expiring contract to the next |
| **Margin** | The deposit that gives futures their leverage |
| **Option** | Right, not obligation, to trade at a fixed price |
| **Call (CE) / Put (PE)** | Right to buy / right to sell |
| **Strike (K)** | The fixed price in the contract |
| **Premium** | The price of the option itself |
| **Writer** | The seller of an option; takes the fee and the open-ended risk |
| **ITM / ATM / OTM** | In / at / out of the money |
| **Lot size** | Shares per contract; revised periodically |
| **Expiry / DTE** | When the contract dies / days remaining |
| **Option chain** | The full grid of strikes and types for one symbol |

[← Who trades](02-who-trades-and-whales.md) · [Index](README.md) · Next: [Open interest →](04-open-interest-the-footprint.md)
