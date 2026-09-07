# 01 — What a Market Actually Is

[Index](README.md) · Next: [Who trades →](02-who-trades-and-whales.md)

Assumed knowledge: none. If you know what a share is and how an exchange works,
skim to [§5](#5-what-actually-moves-a-price).

---

## 1. A company, cut into pieces

Reliance Industries is worth some amount of money. Rather than being owned by
one person, that ownership is cut into identical pieces called **shares** —
**13,301,834,778** of them as at its 30 June 2026 filing, which is a number this
project holds in its own `shareholding.total_shares` column and you can query
yourself. Own one share and you own one 13.3-billionth of the company: a claim
on its profits, and a vote at its meetings.

Two numbers follow immediately, and both matter enormously later:

- **Share price** — what one piece costs right now.
- **Shares outstanding** — how many pieces exist in total.

Multiply them and you get the **market capitalisation** — what the market thinks
the whole company is worth.

> **Why this project cares:** when we later ask "how much of this company did
> mutual funds buy last quarter?", the honest unit is *shares*, expressed as a
> fraction of shares outstanding — not rupees, and not percentages of the
> shareholder register. A company that issues new shares changes everybody's
> percentage without anyone having traded. This distinction caused a real bug;
> see [document 15](15-bugs-and-lessons.md).

## 2. The exchange

If you want to buy a share, you need someone who wants to sell one. An
**exchange** is the place where those two people find each other. India has two
of consequence; this project uses the **National Stock Exchange (NSE)**, the
larger one.

You do not walk into the NSE. You place an order through a **broker**, who
forwards it to the exchange's computers, which match it against somebody else's
opposite order. The market is entirely electronic, opens at 09:15 and closes at
15:30 on weekdays, and is closed on a published list of holidays.

```
     YOU                 BROKER              NSE MATCHING ENGINE
   "buy 10  "  ────────▶  order  ─────────▶   ┌──────────────────┐
    RELIANCE             routed              │  BUY orders      │
    at 1290"                                 │   1291 × 50      │
                                             │   1290 × 200 ◀── you
                                             │  ────────────    │
                                             │  SELL orders     │
                                             │   1290 × 120 ◀── matched!
                                             │   1291 × 800     │
                                             └──────────────────┘
```

The highest price a buyer will pay is the **bid**; the lowest a seller will
accept is the **ask**. A trade happens when they meet. The gap between them is
the **spread**, and a market where that gap is small and there are lots of orders
on both sides is called **liquid**. Liquidity is what lets you buy without
moving the price against yourself — and its absence is precisely the problem a
large investor faces (document 02).

## 3. The regulator

**SEBI** (the Securities and Exchange Board of India) makes the rules. Two of
its rules end up being load-bearing for this project:

- **Quarterly shareholding disclosure.** Every listed company must file, four
  times a year, a breakdown of who owns it: promoters, foreign institutions,
  domestic institutions, mutual funds, insurers, the public. The filing is due
  within 21 days of the quarter ending, and NSE publishes it a few weeks after
  that. **This filing is the only ground truth this project has**, and it is the
  reason document 12's Stage 5 exists.
- **Bulk and block deal disclosure.** Any single trade above certain thresholds
  must be reported by end of day, with the buyer named. This *sounds* like a
  perfect data source and turns out to be useless for our purpose; document 13
  explains why.

## 4. The two markets: spot and derivatives

When you buy a share and own it, that is the **spot market** (or "cash market").
The price you pay is the **spot price**, written **S** everywhere in this
project's code and documentation.

A **derivative** is a contract whose value is *derived from* a share price
without you ever owning the share. NSE runs a very large derivatives market
called **F&O** — Futures and Options. Document 03 explains them from scratch.

**This project uses almost no spot data.** Almost everything comes from F&O,
for a reason that is the project's central bet:

> The spot market tells you *what* happened to a price.
> The derivatives market tells you a great deal more about *who* did it and
> *how convinced they were*.

## 5. What actually moves a price

A price is not a fact about a company. It is the outcome of a queue.

If more people want to buy than sell at ₹1,290, the buyers at ₹1,290 get filled,
the queue empties, and the next trade happens at ₹1,291. Repeat. The price goes
up because **demand exceeded the supply available at that price**, not because
anything about Reliance changed in that second.

This has a consequence that most beginners find counter-intuitive and that
underpins this entire project:

> **A big buyer pushes the price up simply by being big.**

If you want to buy ₹500 crore of a mid-cap stock and only ₹20 crore is on offer
near the current price, you will consume the entire visible queue and keep
climbing. By the time you are done you have paid several percent more than when
you started — and, worse, you have advertised your intentions to everyone
watching. This is **market impact**, and avoiding it is the single biggest
operational problem a large fund has.

```
        A SMALL BUYER                      A LARGE BUYER
   wants 100 shares @ ~1290        wants 4,000,000 shares @ ~1290

   sell queue:                     sell queue:
     1290 × 200  ◀ done              1290 ×   200  ◀ eaten
     1291 × 800                      1291 ×   800  ◀ eaten
     1292 × 1,500                    1292 × 1,500  ◀ eaten
                                     1295 × 4,000  ◀ eaten
                                     1310 × ...    ◀ still buying
   price after:  1290                price after:  1330 and climbing
   anyone notice? no                 anyone notice? everyone
```

Everything in document 02 follows from this picture.

## 6. Vocabulary you now own

| Term | Meaning |
|---|---|
| **Share** | One divisible piece of ownership in a company |
| **Shares outstanding** | How many pieces exist |
| **Market cap** | Price × shares outstanding |
| **Spot price (S)** | The current price of the actual share |
| **Exchange / NSE** | Where buyers and sellers are matched |
| **SEBI** | The regulator; the source of our only ground-truth data |
| **Bid / ask / spread** | Best buy price / best sell price / the gap |
| **Liquidity** | How much you can trade without moving the price |
| **Market impact** | How much your own buying pushes the price against you |
| **Derivative / F&O** | A contract whose value derives from a share price |

[Index](README.md) · Next: [Who trades →](02-who-trades-and-whales.md)
