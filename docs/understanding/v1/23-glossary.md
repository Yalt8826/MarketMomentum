# 17 — Glossary

[← Forward validation](22-forward-validation.md) · [Index](README.md) · Next: [The rest of the repo →](24-the-rest-of-the-repo.md)

Every term used anywhere in this folder, alphabetically. The document where it is
introduced properly is linked.

---

## A

**Accumulation** — building a large position gradually over weeks or months, to
avoid market impact. The project's target quantity.
[02](02-who-trades-and-whales.md)

**Adjusted price** — a price series back-adjusted for splits, bonuses and
dividends. Correct for returns and labels; **wrong** for strike selection.
[08](08-the-data.md)

**ATM (at the money)** — a strike at or near the current stock price.
[03](03-futures-and-options.md)

**AUC** — area under the ROC curve. Ranking quality for a yes/no question; 0.5 is
a coin flip. [11](11-machine-learning-basics.md)

## B
**Basis** — futures price minus spot price. [03](03-futures-and-options.md)

**`basis_excess`** — annualised basis minus the interest rate, i.e. the premium
over fair cost of carry. **The project's one durable feature.**
[04](04-open-interest-the-footprint.md), [10](10-the-features.md)

**Bhavcopy** — NSE's official end-of-day file of every derivatives contract.
From *bhav*, Hindi for price. [08](08-the-data.md)

**Bid / ask / spread** — best buy price / best sell price / the gap between them.
[01](01-what-is-a-market.md)

**Bisection** — the numerical method that inverts Black-Scholes to find implied
volatility. [05](05-pricing-iv-and-greeks.md)

**Black-Scholes** — the formula that prices an option from five inputs.
[05](05-pricing-iv-and-greeks.md)

**Block deal** — a large single trade reported to the exchange. Reaches large
caps but rare (~2/day). [08](08-the-data.md)

**Bonferroni correction** — multiplying a p-value by the number of hypotheses
tested. [11](11-machine-learning-basics.md)

**Bootstrap** — resampling data many times to get a confidence interval.
[11](11-machine-learning-basics.md)

**Breakeven slippage** — the per-leg fill cost at which a strategy's edge
reaches zero. Reported instead of a slippage estimate, because bhavcopy carries
no bid/ask. Measured at **3.16% per leg**. [18](18-what-a-trade-costs.md)

**Brokerage** — a broker's flat charge per order (₹20 assumed here). The
largest single cost component at **57.9%**, because the strategy places 10–12
orders per trade. [18](18-what-a-trade-costs.md)

**Buildup quadrant** — OI direction crossed with price direction, giving an
open-interest build a *direction*. [04](04-open-interest-the-footprint.md),
[10](10-the-features.md)

**Bulk deal** — a trade above 0.5% of listed shares, reported with the buyer
named. Structurally selects microcaps, so it misses the F&O universe.
[08](08-the-data.md)

## C
**Call option (CE)** — the right to buy at a fixed price.
[03](03-futures-and-options.md)

**Causal normalisation** — scaling a feature using only its own past, never the
whole dataset. [11](11-machine-learning-basics.md)

**Charm** — how delta changes as time passes.
[05](05-pricing-iv-and-greeks.md)

**`CHG_IN_OI`** — day-over-day change in open interest. **The single most
important raw field in this project.** [04](04-open-interest-the-footprint.md)

**Client** — NSE's category for retail and small traders.
[02](02-who-trades-and-whales.md)

**Contemporaneous** — measured over the same period, not forecast forward.
[12](12-the-models.md)

**Corporate action** — a split, bonus, buyback or dividend. Screened for, and
*blanked* rather than dropped, to avoid survivorship bias. [09](09-the-pipeline.md)

**Cost of carry** — the interest component that makes a fair futures basis
positive. [03](03-futures-and-options.md)

**Credit** — the premium received for writing an option, in rupees. "Net
credit" is after entry costs. [18](18-what-a-trade-costs.md)

**Credit-to-margin** — net credit divided by margin blocked. What the daily
screen orders by; an efficiency measure, **not** a prediction.
[21](21-the-daily-screen.md)

## D
**Dealer** — a market maker who writes options and hedges the resulting exposure.
Proxied here by NSE's `Pro` category. [02](02-who-trades-and-whales.md),
[06](06-how-derivatives-move-markets.md)

**`DEALER_SIGN`** — the sign convention for dealer positions. `{CE: −1, PE: +1}`
here — **inverted from the US default, and measured**.
[05](05-pricing-iv-and-greeks.md)

**Decile spread** — average outcome of the top 10% minus the bottom 10% by your
ranking. [14](14-results.md)

**Degenerate chain** — a symbol-day where the underlying's price was written
into every option's premium. 47 symbol-days; 83% fall on an expiry date; caused
the backtest's impossible −1,677% trade. [15](15-bugs-and-lessons.md)

**Delta (Δ)** — how much an option moves when the stock moves ₹1; equivalently,
the share-equivalent exposure. [05](05-pricing-iv-and-greeks.md)

**Derivative** — a contract whose value derives from something else.
[01](01-what-is-a-market.md), [03](03-futures-and-options.md)

**DEX** — delta exposure; the OI-weighted net delta of the chain.
[10](10-the-features.md)

**DII** — Domestic Institutional Investor. Indian mutual funds, insurers,
pensions. [02](02-who-trades-and-whales.md)

**`dist_to_flip`** — distance from spot to the gamma-flip level. The best dealer
feature, and invariant to the sign convention. [10](10-the-features.md)

**Drawdown** — the peak-to-trough fall in a book's cumulative value. The
number that makes people stop. Measured here at **−37.27%**, and unmitigated.
[20](20-does-it-make-money.md)

**DTE** — days to expiry. [03](03-futures-and-options.md)

## E

**Embargo** — a purged gap between train and test data so the label horizon
cannot straddle the boundary. [11](11-machine-learning-basics.md)

**ETL** — Extract, Transform, Load. Here: the stage that computes IV, Greeks and
chain snapshots into `option_metrics`. [09](09-the-pipeline.md)

**Expiry** — when a contract dies. Indian stock F&O expires monthly.
[03](03-futures-and-options.md)

## F

**F&O** — Futures and Options; NSE's derivatives market.
[01](01-what-is-a-market.md)

**Feature** — an input number computed from raw data.
[10](10-the-features.md), [11](11-machine-learning-basics.md)

**FII** — Foreign Institutional Investor. **Their accumulation is a price-momentum
phenomenon** (IC +0.360). [02](02-who-trades-and-whales.md), [14](14-results.md)

**Future** — an obligation to trade at a fixed price on a fixed date.
[03](03-futures-and-options.md)

## G
**Gamma (Γ)** — how fast delta changes. Drives forced dealer hedging.
[05](05-pricing-iv-and-greeks.md)

**Gamma flip** — the price at which net dealer gamma crosses zero.
[05](05-pricing-iv-and-greeks.md)

**Gate** — a pre-registered test with a numeric pass mark fixed before the
result exists, that can stop the work, and whose outcome is published at the
same resolution either way. [19](19-the-four-gates.md)

**GEX** — gamma exposure. The project's second abandoned hypothesis.
[06](06-how-derivatives-move-markets.md), [10](10-the-features.md)

**Greeks** — delta, gamma, theta, vega, rho, plus vanna and charm.
[05](05-pricing-iv-and-greeks.md)

## H

**Health gate** — `verify_db_health.py`; a structural check that stops the
pipeline on failure. [09](09-the-pipeline.md)

**HMM (Hidden Markov Model)** — a model assuming data passes through hidden
states. Stage 2; found no persistent states. [12](12-the-models.md)

## I
**IC (Information Coefficient)** — rank correlation between a prediction and the
outcome. The project's main metric. [11](11-machine-learning-basics.md)

**ICIR** — mean(IC) ÷ std(IC). [11](11-machine-learning-basics.md)

**Identifiability** — whether the data determines a unique answer. Stage 3's
per-symbol allocation is **not** identifiable. [12](12-the-models.md)

**In-sample** — measured on the same data used to choose the method. Every
Gate 0 and Gate 1 number is in-sample; Gate 3 is the remedy.
[19](19-the-four-gates.md)

**ITM (in the money)** — a strike already profitable to exercise.
[03](03-futures-and-options.md)

**IV (Implied volatility)** — expected movement, backed out of an option's price.
Also the project's biggest nuisance variable.
[05](05-pricing-iv-and-greeks.md)

## L
**Label** — the answer a model is trained against. **This project's central
problem is that no per-symbol institutional label exists** in daily data.
[07](07-the-project-idea.md)

**Leakage** — information from the future reaching the model.
[11](11-machine-learning-basics.md)

**LLP (Learning from Label Proportions)** — learning when only group totals are
observed. Stage 3. [11](11-machine-learning-basics.md)

**Lot size** — the fixed bundle of shares one derivatives contract covers; you
cannot trade a fraction of one. Set per symbol by NSE and revised over time (852
change-points here). Converts points per share into rupees.
[03](03-futures-and-options.md) · [18](18-what-a-trade-costs.md)

## M
**Margin** — collateral the exchange requires from an option writer, held for
the life of the position and returned on close. Not a cost, but the correct
denominator for return. Modelled here as 12% of contract notional — an
approximation, swept ±25%. [18](18-what-a-trade-costs.md)

**Market impact** — the price damage your own size does to you.
[01](01-what-is-a-market.md)

**Max pain** — the strike at which the most option value expires worthless.
[10](10-the-features.md)

**MF** — actively managed mutual fund. **The holder the project's one positive
result is about.** [02](02-who-trades-and-whales.md), [14](14-results.md)

**MIFOR** — the Indian interest-rate benchmark used as *r* in Black-Scholes.
[08](08-the-data.md)

**Momentum** — recent price trend. The mandatory baseline for any per-symbol
accumulation claim. [07](07-the-project-idea.md)

**Moneyness** — where the strike sits relative to the current price.
[03](03-futures-and-options.md)

## N

**Nowcast** — detecting what is happening now, rather than forecasting what will
happen. [07](07-the-project-idea.md), [12](12-the-models.md)

**Notional** — the rupee value of a position; here `oi × spot`, with no lot
factor. [04](04-open-interest-the-footprint.md)

**NSE** — National Stock Exchange of India. [01](01-what-is-a-market.md)

## O

**OI (Open interest)** — contracts currently open and carrying risk. Not volume.
[04](04-open-interest-the-footprint.md)

**Outlier strike** — a strike whose OI change is extreme against the other
strikes of the same expiry that day. [10](10-the-features.md)

**OTM (out of the money)** — a strike not yet profitable to exercise. Cheap, so
where leveraged bets show up. [03](03-futures-and-options.md)

## P
**Partial correlation** — correlation remaining after a control variable is
removed. **The project's decisive tool.**
[11](11-machine-learning-basics.md)

**Participant OI** — NSE's daily named breakdown (FII/DII/Pro/Client). Named but
market-wide, therefore blind to symbols. [08](08-the-data.md)

**Placebo** — re-running on shuffled labels; the score should collapse to zero.
[11](11-machine-learning-basics.md)

**Pre-registration** — writing the success criterion down before seeing the
result. Borrowed from clinical trials, for the same reason.
[19](19-the-four-gates.md)

**Premium** — the price of an option. [03](03-futures-and-options.md)

**Premium residual** — an option's price change with the underlying's own
contribution removed. [04](04-open-interest-the-footprint.md)

**Pro** — NSE's category for proprietary desks; the dealer proxy.
[02](02-who-trades-and-whales.md)

**Purged walk-forward** — expanding time splits with an embargo gap.
[11](11-machine-learning-basics.md)

**Put option (PE)** — the right to sell at a fixed price.
[03](03-futures-and-options.md)

## R

**Raw / unadjusted price** — the price actually printed that day. Correct for
strikes, IV, Greeks and basis. [08](08-the-data.md)

**Ridge regression** — linear regression with a penalty that stops one feature
dominating. [11](11-machine-learning-basics.md)

**Roll** — moving a position from the expiring contract to the next.
[03](03-futures-and-options.md)

**RV (Realised volatility)** — how much a price actually moved. Estimated here
with Yang-Zhang. [05](05-pricing-iv-and-greeks.md)

## S
**SEBI** — the Indian securities regulator; source of the quarterly shareholding
filings that are this project's only ground truth.
[01](01-what-is-a-market.md), [08](08-the-data.md)

**Settlement price** — the exchange's official daily mark, which exists even if
the contract never traded. [08](08-the-data.md)

**Shareholding filing** — SEBI's quarterly per-symbol ownership breakdown.
**The label.** [08](08-the-data.md)

**Sharpe ratio** — return divided by volatility. Quoted here (2.16) and
discounted: it systematically flatters strategies whose risk sits in a rare left
tail. [20](20-does-it-make-money.md)

**Short strangle** — selling an out-of-the-money call and an out-of-the-money
put on the same underlying and expiry. Profits if the stock stays between the
strikes; loses on a large move in either direction. The strategy of Part III.
[17](17-selling-volatility.md)

**SIP** — Systematic Investment Plan; monthly retail money flowing into mutual
funds. [02](02-who-trades-and-whales.md)

**Slippage** — the gap between the price you assume and the price you get.
Unmeasurable here, because bhavcopy publishes no bid or ask. See *breakeven
slippage*. [18](18-what-a-trade-costs.md)

**SPAN** — the exchange's real margin calculation, from a daily risk array NSE
does not publish. Cannot be reproduced from bhavcopy; approximated instead.
[18](18-what-a-trade-costs.md)

**Spot price (S)** — the current price of the actual share.
[01](01-what-is-a-market.md)

**Stop loss** — closing a position once a loss reaches a set size. Measured at
1.5×/2×/3× the entry credit and **rejected**: it worsened return, worst trade,
drawdown and Sharpe simultaneously. [20](20-does-it-make-money.md)

**Strike (K)** — the fixed price written into an option contract.
[03](03-futures-and-options.md)

**STT** — Securities Transaction Tax; 0.1% of premium turnover on the **sell
side only**. Rose from 0.0625% in October 2024 — one of two candidate
explanations for the strategy's decaying edge. [18](18-what-a-trade-costs.md)

## T

**Theta (Θ)** — daily time decay. [05](05-pricing-iv-and-greeks.md)

**Tripwire** — a model deliberately given the answer; should score perfectly, or
the harness is broken. [11](11-machine-learning-basics.md)

**`total_shares`** — shares outstanding; the denominator that makes accumulation
comparable across company sizes. [07](07-the-project-idea.md)

## U

**UDiFF** — NSE's newer bhavcopy file format, from 2024-07-08. Mapped back to
legacy column names on ingest. [08](08-the-data.md)

## V
**Vanna** — how delta changes when volatility changes.
[05](05-pricing-iv-and-greeks.md)

**Variant** — a labelled parameter set within one backtest run (`base`,
`stop1.5`, …). Setting it late rather than at row creation merged every
variant's skipped months into the base run. [15](15-bugs-and-lessons.md)

**Vega (ν)** — sensitivity to a change in volatility.
[05](05-pricing-iv-and-greeks.md)

**Volatility risk premium** — the tendency of option buyers to pay slightly
more than an option's expected cost, because insurance is worth more to a hedger
than its fair price. The economic source of the strangle strategy's return.
[17](17-selling-volatility.md)

**Volume** — contracts traded today, including churn. Not open interest.
[04](04-open-interest-the-footprint.md)

## W
**Walk-forward** — repeated expanding train/test splits through time: choose
the method using only data prior to each period, score it on the next. The
honest version of a backtest; Gate 1's walk-forward was negative for five of six
features. [11](11-machine-learning-basics.md) · [19](19-the-four-gates.md)

**Whale** — a participant large enough that their trading is itself an event.
[02](02-who-trades-and-whales.md)

**Writer** — the seller of an option, who receives the premium and carries the
open-ended obligation. The insurer side of the trade. Institutional desks are
net writers of calls on **99.9%** of days.
[03](03-futures-and-options.md) · [17](17-selling-volatility.md)

## X

**XBRL** — the structured XML format SEBI shareholding filings use.
[08](08-the-data.md)

## Y

**Yang-Zhang** — a realised-volatility estimator using open, high, low and close.
[05](05-pricing-iv-and-greeks.md)

## Z

**Z-score, robust** — a spread measure resistant to outliers, used by the
outlier-strike detector. [10](10-the-features.md)

**Z-score, causal within-symbol** — scaling against a symbol's own expanding past.
Correct for comparability, and **wrong for measuring persistence** — it produced
the project's largest and most spurious number. [15](15-bugs-and-lessons.md)

[← Forward validation](22-forward-validation.md) · [Index](README.md) · Next: [The rest of the repo →](24-the-rest-of-the-repo.md)
