# Zone Agent Evidence Pack — Foundation 1

Prepared: 2026-08-29

Purpose: provide credible source material for generating testable hypotheses about NIFTY supply/demand zones. This document is research input, not a set of live trading rules. Every hypothesis must be evaluated point-in-time on NIFTY data, after execution costs, before it can influence live decisions.

## Evidence standard

- `strong`: peer-reviewed or authoritative institutional research with direct empirical evidence.
- `moderate`: credible empirical research, but from a different market, instrument, or horizon.
- `methodological`: governs how hypotheses are tested rather than predicting price direction.
- No source below proves profitability for five-minute NIFTY options.

## 1. Orders cluster near visible support/resistance levels

Source: Carol L. Osler, *Currency Orders and Exchange-Rate Dynamics: Explaining the Success of Technical Analysis*, Federal Reserve Bank of New York Staff Report 125; later published in the Journal of Finance.

URL: https://www.newyorkfed.org/research/staff_reports/sr125.html

Evidence: The study examined stop-loss and take-profit orders at a large FX dealing bank. Orders clustered at round numbers. Take-profit orders were associated with reversals at levels, while stop-loss orders could intensify movement after a level was crossed.

Applicability: moderate. The mechanism is relevant to order-driven markets, but the source studies foreign exchange rather than NIFTY.

Testable hypotheses:

- `Z01`: Zones whose proximal or distal boundary lies near a salient NIFTY round number have a different target-before-stop rate than other zones.
- `Z02`: A clean close beyond the distal boundary predicts continuation better than an immediate second reversal attempt.
- `Z03`: Rejection and breakout are competing outcomes: zone strength should be modeled jointly with post-break acceleration, not as reversal-only logic.

Required features: distance to nearest 50/100-point level, boundary type, close beyond distal in ATR units, post-break MFE, touch count.

## 2. Limit-order clustering can create temporary price barriers

Source: Alexis Cellier and David Bourghelle, *Limit Order Clustering and Price Barriers on Financial Markets: Empirical Evidence from Euronext*.

URL: https://papers.ssrn.com/abstract=966454

Evidence: Limit orders clustered at prominent price increments in Euronext data, with accumulated depth producing temporary price barriers. Strategic traders also stepped ahead of clustered prices to obtain priority.

Applicability: moderate. This is an electronic limit-order market, but not NSE and not index options.

Testable hypotheses:

- `Z04`: Exact round-number entries underperform entries placed slightly before the level because of queueing and adverse selection.
- `Z05`: Zones aligned with observable depth concentration have higher rejection probability than candle-only zones.
- `Z06`: The useful entry offset varies with spread, volatility, and tick/price granularity rather than being a fixed number of NIFTY points.

Required data: best bid/ask, depth by price, spread, tick size, order imbalance, entry offset, fill probability.

## 3. Short-horizon reversal may represent temporary liquidity imbalance

Source: Steven L. Heston, Robert A. Korajczyk and Ronnie Sadka, *Intraday Patterns in the Cross-section of Stock Returns*, Journal of Finance 65(4), 2010.

URL: https://onlinelibrary.wiley.com/doi/10.1111/j.1540-6261.2010.01573.x

Evidence: The paper documents intraday return patterns and reports that very short-term reversal is connected to temporary liquidity imbalance and bid-ask bounce.

Applicability: moderate-to-low for direct prediction. The study concerns cross-sectional US equity returns, but it supplies a credible mechanism for temporary reversal.

Testable hypotheses:

- `Z07`: Zone reversals accompanied by a temporary volume/imbalance shock mean-revert more often than identical candle patterns without such a shock.
- `Z08`: Apparent small zone profits disappear when evaluated at bid/ask rather than candle midpoints.
- `Z09`: Zone holding period should be conditional on the expected duration of the liquidity shock; late entries may have no remaining edge.

Required data: signed volume/order imbalance, spread, volume anomaly, signal latency, MFE/MAE by minute.

## 4. Technical rules require data-snooping correction

Source: Po-Hsuan Hsu and Chung-Ming Kuan, *Reexamining the Profitability of Technical Analysis with Data Snooping Checks*, Journal of Financial Econometrics 3(4), 2005.

URL: https://academic.oup.com/jfec/article-abstract/3/4/606/907780

Evidence: The authors evaluate a broad universe of technical rules using White's Reality Check and Hansen's SPA test. Results vary across markets, and costs materially affect conclusions.

Applicability: methodological, strong.

Training requirements:

- `M01`: Record every tested zone rule and parameter combination.
- `M02`: Do not promote a rule solely because it is the best performer among many trials.
- `M03`: Evaluate net returns after all costs on chronological out-of-sample periods.

## 5. Backtest selection inflates performance

Source: David H. Bailey, Jonathan Borwein, Marcos López de Prado and Qiji Jim Zhu, *The Probability of Backtest Overfitting*.

URL: https://papers.ssrn.com/sol3/Papers.cfm?abstract_id=2326253

Evidence: Selecting strategies from many historical trials can produce apparently strong backtests that degrade out of sample. The paper provides a framework for estimating this risk.

Applicability: methodological, strong.

Training requirements:

- `M04`: Keep an experiment ledger including rejected variations.
- `M05`: Use chronological or combinatorially symmetric validation appropriate to overlapping financial observations.
- `M06`: Keep a final untouched period that is not used for source extraction, feature selection, or threshold selection.

## 6. Sharpe ratios require correction for selection and non-normal returns

Source: David H. Bailey and Marcos López de Prado, *The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality*, Journal of Portfolio Management 40(5), 2014.

URL: https://papers.ssrn.com/sol3/Delivery.cfm/SSRN_ID2460551_code87814.pdf?abstractid=2460551

Evidence: The Deflated Sharpe Ratio adjusts observed performance for multiple trials and non-normal returns.

Applicability: methodological, strong.

Training requirements:

- `M07`: Report ordinary and deflated Sharpe alongside expectancy, drawdown, hit rate, payoff ratio, and sample size.
- `M08`: Treat a high in-sample Sharpe with few trades or many attempted variants as weak evidence.

## 7. Algorithmic execution requires explicit operational controls

Source: Securities and Exchange Board of India, algorithmic-trading guidance and consolidated risk-control material.

URLs:

- https://www.sebi.gov.in/sebi_data/commondocs/oct-2023/Chapter-2-Trading_Software_and_Technology_p.pdf
- https://www.sebi.gov.in/sebi_data/attachdocs/jun-2025/1750158789381.pdf

Evidence: SEBI material emphasizes order-level price and quantity checks, position limits, monitoring, and mechanisms for stopping dysfunctional or runaway algorithms.

Applicability: operational, strong for Indian automation. Confirm current broker/exchange requirements before live deployment.

Agent requirements:

- `R01`: The learning agent must never bypass deterministic price, quantity, exposure, daily-loss, or kill-switch controls.
- `R02`: Every automated order must be traceable to a strategy version, memory version, signal, and risk decision.
- `R03`: An unavailable or malformed AI decision must not expand trading authority.

## Initial research priority

The first model should compare four mutually exclusive outcomes for every detected zone:

1. no executable entry;
2. entry followed by reversal target before stop;
3. entry followed by distal break/stop;
4. distal break followed by continuation.

This framing directly tests the reversal-versus-breakout mechanism described in the microstructure evidence. It is more informative than training only on `win` and `loss`.

## Promotion rule

An evidence-derived hypothesis may influence live scoring only after:

1. point-in-time feature construction;
2. adequate observations in development data;
3. chronological out-of-sample improvement over the unmodified baseline;
4. positive expectancy after realistic execution costs;
5. shadow/paper confirmation;
6. documented limitations and rollback criteria.

