# 05 — Pricing, Implied Volatility and the Greeks

[← Open interest](04-open-interest-the-footprint.md) · [Index](README.md) · Next: [How derivatives move markets →](06-how-derivatives-move-markets.md)

Options have a price. Where does it come from? The answer produces a family of
numbers — the *Greeks* — that turn out to describe **forced, mechanical trading
in the actual stock**, which is the mechanism the whole project originally bet
on.

No mathematics beyond "a formula takes inputs and returns a number" is assumed.

---

## 1. What an option is worth

Reliance is at ₹1,290. What should a one-month ₹1,400 call cost?

It depends on how likely it is that Reliance gets above ₹1,400 in a month, and
how far above. That in turn depends on five things:

| Input | Symbol | Observable? |
|---|---|---|
| Current stock price | S | ✅ yes |
| Strike price | K | ✅ yes |
| Time until expiry | T | ✅ yes |
| Interest rate | r | ✅ yes |
| **How much the stock moves around** | **σ** (sigma) | ❌ **no** |

The first four you can look up. The fifth — **volatility** — is the whole
problem. Nobody publishes how much a stock is *going* to move.

## 2. Black-Scholes

In 1973 Fischer Black and Myron Scholes wrote down a formula that turns those
five inputs into a price. You do not need to read it, but here it is so it is not
a black box:

```
   Call price = S · N(d₁) − K · e^(−rT) · N(d₂)

   where  d₁ = [ ln(S/K) + (r + σ²/2)·T ] / (σ·√T)
          d₂ = d₁ − σ·√T
          N() is the cumulative normal distribution
```

The project implements this in `src/mktmtm/options/pricing.py` as pure functions
with no database access — deliberately, so they can be unit-tested against known
values.

## 3. Implied volatility — running the formula backwards

Here is the clever part. We cannot observe σ. But we *can* observe the price the
option actually trades at.

So run the formula **backwards**: given the price the market pays, what value of
σ would make Black-Scholes produce that price? That answer is the **implied
volatility (IV)** — the market's own estimate of future movement, extracted from
what it is willing to pay.

```
    FORWARD                              BACKWARD (what we do)

    S, K, T, r, σ                        S, K, T, r, market price
        │                                    │
        ▼                                    ▼
    Black-Scholes                        guess σ, price it,
        │                                compare, narrow the range,
        ▼                                repeat ~50 times
      price                                  │
                                             ▼
                                        implied volatility
```

There is no closed-form way to invert the formula, so it is solved numerically by
**bisection** — guess a volatility, price the option, see whether you overshot,
halve the search range, repeat.

> ⚠️ **A rule this project enforces absolutely:** when bisection does not
> converge — a degenerate input, a price outside the possible range — the answer
> is **`None`, never a number**. An earlier version returned `0.0` or `500.0` for
> those cases, and those fake values propagated into every downstream volatility
> feature. See [document 15](15-bugs-and-lessons.md).

### 3.1 Implied vs realised volatility

- **Realised volatility (RV)** — how much the stock *actually* moved, measured
  from history. This project uses the **Yang-Zhang** estimator, which uses each
  day's open, high, low and close rather than just closes, and is far more
  statistically efficient as a result.
- **Implied volatility (IV)** — how much the market *expects* it to move.

The gap between them (the *variance risk premium*) is what option writers earn
on average, and is why writing options is a viable business.

### 3.2 IV is also the project's biggest nuisance

IV is the single strongest predictor of near-term movement in this dataset. That
sounds good and is actually a problem:

> **A new feature can look impressive while merely rediscovering IV.**

Hence a standing rule that runs through everything: **every feature is judged on
its *partial* correlation after IV (or price momentum) is controlled for**, never
on its raw correlation. Document 11 explains what a partial correlation is;
document 14 shows several features that looked good and died on that test.

## 4. The Greeks

The Greeks measure how an option's price reacts to each thing that can change.
They are named after Greek letters (mostly).

| Greek | Answers | Why this project cares |
|---|---|---|
| **Delta** (Δ) | If the stock moves ₹1, how much does the option move? | It is *exactly* how many shares a dealer must hold to be hedged. Between 0 and 1 for calls, 0 and −1 for puts. |
| **Gamma** (Γ) | How fast does delta itself change? | **The key one.** High gamma means the hedge must be constantly readjusted — forced, repeated trading in the stock. |
| **Theta** (Θ) | How much value decays per day? | The writer's income; the buyer's bleed. |
| **Vega** (ν) | Sensitivity to a change in volatility | Links option positioning to volatility expectations. |
| **Rho** (ρ) | Sensitivity to interest rates | Minor here; computed for completeness. |
| **Vanna** | How delta changes when *volatility* changes | Second-order hedging pressure. |
| **Charm** | How delta changes as *time* passes | Hedging that must happen even if nothing moves at all. |

### 4.1 Delta, concretely

An at-the-money call has a delta of about 0.5: if Reliance rises ₹1, the option
rises about ₹0.50. A deep out-of-the-money call has a delta near 0 — the stock
barely affects it. A deep in-the-money call has a delta near 1 — it moves
rupee-for-rupee with the stock, because it is essentially the stock.

Delta is therefore **the share-equivalent of the option**. Own a call with delta
0.5 on a 500-share lot and you have the economic exposure of 250 shares.

**This is the number the delta trap in document 04 §4 was about.** The reason a
call's price rises on an up day, with no trading, is delta.

### 4.2 Gamma, and why it is the centre of the original theory

Gamma is how fast delta changes. It matters because of what a dealer has to do.

Suppose a market maker has **written** (sold) calls to a retail crowd. They do
not want a directional bet — they want the fee. So they buy shares to offset the
delta of what they sold, and they stay hedged.

But delta moves. As the stock rises, the calls they sold get closer to the
money, their delta rises, and the dealer must **buy more shares** to stay
hedged. As it falls, they must **sell**.

```
   A DEALER WHO IS SHORT GAMMA (has written options)

   stock rises  →  delta of their short calls rises  →  MUST BUY shares
   stock falls  →  delta falls                       →  MUST SELL shares

                   they trade WITH the move → amplifies it

   A DEALER WHO IS LONG GAMMA (has bought options)

   stock rises  →  MUST SELL     ┐
   stock falls  →  MUST BUY      ┘ trades AGAINST the move → damps it
```

That trading is **not a choice**. It follows from the arithmetic of their
position. It is the closest thing in finance to a physical law, and it is why
this project's original hypothesis was:

> Measure the net gamma dealers hold across all strikes — **gamma exposure**,
> or **GEX** — and you know whether their hedging will amplify or damp tomorrow's
> moves.

**The project tested that, and it did not hold.** Document 14 §5 gives the full
result. The mechanism is real; the tradeable prediction is not, at least not in
this market on this data.

### 4.3 Which way do Indian dealers lean?

The US convention assumes dealers are long calls and short puts. This project
**measured** the Indian case instead of assuming it, using NSE's participant
data, and found the opposite for single stocks:

| Measurement | Result |
|---|---|
| Days the `Pro` book was net **short** calls | **99.9%** of 1,258 |
| Days it was net **long** puts | **98.3%** |

So the sign constant in the code is `{"CE": −1.0, "PE": +1.0}` — inverted from
the textbook. Under that convention, **dealers are short gamma on 97.3% of
symbol-days**, which is what a market where retail buys calls and institutions
write them should look like.

For *index* options no fixed sign is correct at all: the Pro index book flipped
sign monotonically across 2024–25 (net short calls on 90.5% of days in 2021,
36.7% in 2026). Indices are therefore excluded from dealer-exposure claims
rather than assigned a wrong constant.

## 5. Vocabulary

| Term | Meaning |
|---|---|
| **Black-Scholes** | The formula that prices an option from five inputs |
| **Volatility (σ)** | How much a price moves around |
| **Implied volatility (IV)** | Volatility backed out of an option's market price |
| **Realised volatility (RV)** | Volatility measured from actual past movement |
| **Yang-Zhang** | The RV estimator used here; uses open/high/low/close |
| **Bisection** | The numerical method that inverts Black-Scholes |
| **Delta (Δ)** | Share-equivalent exposure of an option |
| **Gamma (Γ)** | Rate of change of delta; drives forced hedging |
| **Theta / Vega / Rho** | Sensitivity to time / volatility / interest rates |
| **Vanna / Charm** | Second-order: delta vs volatility, delta vs time |
| **GEX** | Gamma exposure — net gamma dealers hold |
| **Gamma flip** | The price at which net dealer gamma changes sign |

[← Open interest](04-open-interest-the-footprint.md) · [Index](README.md) · Next: [How derivatives move markets →](06-how-derivatives-move-markets.md)
