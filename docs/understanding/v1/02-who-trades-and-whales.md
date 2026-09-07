# 02 — Who Trades, and What a "Whale" Is

[← Markets](01-what-is-a-market.md) · [Index](README.md) · Next: [Futures and options →](03-futures-and-options.md)

The project's name for its subject is *whale* — a participant large enough that
their trading is a event in itself. This document explains who those
participants actually are in the Indian market, why size is a handicap as much
as an advantage, and why that handicap is what makes the project possible at
all.

---

## 1. The cast

NSE sorts every participant into four buckets and publishes daily totals for
each. These four names appear constantly in this project's code and tables.

| Code | Who they are | Typical size | Behaviour |
|---|---|---|---|
| **FII** | **Foreign Institutional Investors** — overseas funds, pension money, hedge funds investing into India | Very large | Fast, momentum-driven, moves in and out of the whole country at once |
| **DII** | **Domestic Institutional Investors** — Indian mutual funds, insurance companies, pension funds | Very large | Slower, steadier, often buying when foreigners are selling |
| **Pro** | **Proprietary desks** — brokers trading their own money, including the market makers | Large but usually flat | Provides liquidity, wants to be hedged, earns fees not directional bets |
| **Client** | **Everyone else** — retail traders, individuals, small firms | Individually tiny, collectively huge | Buys options, sells rarely, takes the other side of the institutions |

Two sub-groups matter later and are *not* separated by NSE, only by SEBI's
quarterly filings:

- **MF** — actively managed **mutual funds**. Money from Indian savers, much of
  it arriving on a monthly schedule through SIPs (systematic investment plans).
  A fund manager decides where it goes.
- **Insurers** — LIC and the private insurance companies. Also domestic, also
  large, but far slower and much more mechanical.

Both sit inside "DII". Splitting them turns out to be the difference between
finding a signal and not finding one — see document 12, Stage 5.

## 2. Why size is a handicap

Return to the picture at the end of document 01. A fund that wants ₹500 crore of
a mid-cap stock cannot simply buy it. Doing so would:

1. **Cost them money.** Every rupee of price impact is a rupee of return lost.
2. **Advertise the trade.** Other participants see a persistent buyer, buy ahead
   of them, and make the impact worse.

So they do not do it. They spread the purchase over **weeks or months**, buying
a small slice each day, deliberately staying below the level that would be
noticed. This is why the project's target quantity is *accumulation over a
quarter*, not *a purchase on a day*. There is no purchase on a day. There is a
campaign.

```
    WHAT A ₹500 CRORE PURCHASE ACTUALLY LOOKS LIKE

    volume
      │                                       ← the fund's slice, ~5% of daily
      │    ▓░░░░  ▓░░░░  ▓░░░  ▓░░░░░  ▓░░░     volume, invisible on any one day
      │    ▓░░░░  ▓░░░░  ▓░░░  ▓░░░░░  ▓░░░
      └────┴──────┴──────┴─────┴───────┴────▶ time
           Mon    Tue    Wed   Thu     Fri
                    ... for eleven weeks ...
```

## 3. The three problems a whale has, and their solutions

This is the heart of why derivatives data is informative. A large buyer needs to
solve three problems, and each solution leaves a *different* trace.

### Problem 1 — "I need exposure now, but I can only buy slowly."

They want the economics of owning the stock today, even though the physical
purchase will take two months. The clean answer is a **futures contract** or a
**call option**, both of which give you the payoff of ownership immediately, in
one trade, without touching the crowded spot queue.

**Trace left:** futures open interest rises; the futures price gets pushed above
where it should be relative to the stock (the *basis* widens — document 04 §5).

### Problem 2 — "I am now holding a large position I cannot exit quickly."

Being big means being trapped. If the stock falls 15%, they cannot sell into it
any faster than they bought. So they buy **put options** as insurance.

**Trace left:** unusual open-interest builds in out-of-the-money puts.

### Problem 3 — "I have a view, but I want leverage on it."

An out-of-the-money option costs a fraction of the stock and multiplies a
correct call several-fold. A concentrated bet often shows up as an implausibly
large position in one specific far-away strike.

**Trace left:** a single strike whose open interest jumps out of line with every
other strike in the same expiry. This is exactly what the project's
**outlier-strike detector** hunts for (document 10).

## 4. The asymmetry that makes it all legible

Here is the fact that makes the derivatives market so much more informative than
the stock market, and it is a fact this project *measured* rather than assumed:

> **Institutions and retail sit on opposite sides of the option market, almost
> without exception.**

In our own data, across 1,258 trading days:

| Measurement | Result |
|---|---|
| Days institutional desks were net **sellers (writers) of calls** | **99.9%** |
| Days institutional desks were net **buyers of puts** | **98.3%** |

Retail is on the other side of both. That is not a coincidence — document 03
explains why writing options is a professional's trade and buying them is an
amateur's — but the consequence is what matters here: **a feature that can tell
the writer of a contract from its buyer is measuring something real about who is
trading**, not just how much.

## 5. What the data does and does not tell us

NSE publishes, once every evening, exactly how many contracts each of FII, DII,
Pro and Client holds. This is genuinely remarkable — it names the participants.

And it is almost useless, for one reason:

> **It has no symbol breakdown.**

It tells you that foreign institutions bought 50,000 stock call contracts today.
It cannot tell you *in which stock*. There is one row per category per day —
6,290 rows for five years — and when you attach it to a table that has one row
per stock per day, it takes the same value for every stock, and therefore
contributes nothing at all to telling stocks apart.

```
   WHAT WE WANT TO KNOW              WHAT NSE PUBLISHES

   FII flow in RELIANCE on 28 Aug    FII flow in EVERYTHING on 28 Aug
   FII flow in TCS      on 28 Aug         ↑
   FII flow in INFY     on 28 Aug    one number, no symbols

   217 stocks × 1,254 days           1,258 days
   = 212,441 rows                    = 6,290 rows
   NO LABEL EXISTS                   LABEL EXISTS BUT IS BLIND
```

**This mismatch is the project's central problem.** Every modelling stage in
document 12 is a different attempt to work around it, and the one that finally
succeeded did so by finding a *different* label entirely — SEBI's quarterly
filings, which are per-symbol, named, and honest, but arrive only four times a
year and six weeks late.

## 6. Vocabulary

| Term | Meaning |
|---|---|
| **FII** | Foreign Institutional Investor |
| **DII** | Domestic Institutional Investor (mutual funds + insurers + pensions) |
| **MF** | Actively managed mutual fund — a subset of DII, and the one that matters |
| **Pro** | Proprietary desk; the market-maker / dealer proxy |
| **Client** | Retail and small traders |
| **SIP** | Systematic Investment Plan — monthly retail flow into mutual funds |
| **Accumulation** | Building a large position gradually over weeks or months |
| **Distribution** | The opposite — selling one down gradually |
| **Market impact** | The price damage your own size does to you |

[← Markets](01-what-is-a-market.md) · [Index](README.md) · Next: [Futures and options →](03-futures-and-options.md)
