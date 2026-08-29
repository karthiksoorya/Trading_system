# backtest/

Offline strategy research for the Nifty trading system.

**Goal:** measure how much money the demand/supply edge actually makes once you
account for the instrument (futures vs options), theta, IV crush and costs — and
then test whether tweaks or alternative strategies do better.

The strategy code here **calls the production `engine/` functions unchanged**
(`zones.py`, `candle.py`, `signals.py`, `boosters.py`, `confluence.py`). Only the
scan orchestration is re-implemented (`strategy_ds.py`), because the live version
is welded to `datetime.now()`, the DB, the broker and Telegram.

Nothing here touches the live engine, the broker, Telegram or `data/trades.db`.

---

## One-time setup

Uses the project's Python 3.14 (`pandas`, `numpy`, `pyarrow` already installed).
`python` on PATH is 3.12 with no deps — use **`py -3.14`**:

```bash
py -3.14 -m backtest.run ...
```

## Workflow

### 1. Get historical data

Log in to Kite via the dashboard first (the token in `.kite_token` is only good
for the current trading day), then:

```bash
python -m backtest.data.fetch --check          # test API access only
python -m backtest.data.fetch --years 3        # pull 3 years of 5-min data
```

Fetches NIFTY (5/15/60-min), India VIX (5-min + daily) and front-month NIFTY
futures into `backtest/data/cache/*.parquet`. Chunked to respect Kite's
per-request span limits. Re-fetch after a fresh Kite login to extend the range.

If access fails: `PermissionException` = historical data not enabled on the Kite
app; `TokenException` = stale token, log in again.

**No Kite access?** `python -m backtest.data.synth --days 400` writes toy data so
the pipeline runs — but results are meaningless (flagged with `_SYNTHETIC.flag`).

### 2. Calibrate the option model

```bash
python -m backtest.calibrate
```

Fits `option_model.ModelParams` (term premium, IV-crush, theta accel, spread)
against the real option fills in `data/trades_*.db` — 14 "disciplined" trades;
4 known spike-entry / manual-stall trades (`OUTLIER_IDS`) are shown but excluded
from the fit. Prints per-trade actual-vs-modelled premiums. Paste the fitted
params into `option_model.ModelParams` if entry premiums look sane.

### 3. Run backtests

```bash
python -m backtest.run                              # all instruments, full history
python -m backtest.run --from 2024-01-01 --to 2024-06-30
python -m backtest.run --classes supply             # PE only (the side with the edge)
python -m backtest.run --entry-mode market          # live-style fill (vs default limit)
python -m backtest.run --min-risk 20                 # only wider zones
python -m backtest.run --no-trend-filter            # ablation
python -m backtest.run --csv trades.csv             # dump every trade
```

Output: one row per instrument — net P&L, expectancy, profit factor, max
drawdown, Sharpe, and **₹ earned per index point** (the conversion efficiency
this whole exercise is about).

Instruments compared (same signals, same entries/exits, priced differently):
`futures`, `opt_atm`, `opt_itm1` (current live), `opt_itm3` (deep ITM),
`opt_itm1_mono` (monthly expiry).

---

## Findings (3-year real data, Aug 2026)

**The demand/supply edge does not hold as currently traded.**

| Lever | Result |
|---|---|
| **Entry method** | `market` (current live: fire when LTP within 50pt of proximal) → **−4,125 index pts / 3y**. `limit` (rest an order AT the proximal, no trade if price never returns) → **+326 index pts / 3y**. Same signals. Live execution throws ~4,450 pts away. |
| **Direction** | supply/PE: +560 pts over 244 trades (+2.3/trade). demand/CE: **−234 pts over 90 trades — zero edge.** |
| **Costs** | Futures round-trip ≈ ₹490/trade (STT ~₹312). Gross edge with limit entry ≈ ₹42–166/trade. **Cost > edge.** Net −₹330 to −₹450/trade. |
| **Options** | Every variant loses ₹950–1,400/trade, ~3% win rate. No take-profit rule fixes the instrument. |
| **`min_risk_points`** | Weak, noisy positive effect. ~12 and ~25 look best but within noise. Default 10 = noise floor only. |

Bottom line: on this model, no version of these signals is profitable after costs.
The order of the real problems: (1) live entry method, (2) CE has no edge,
(3) costs exceed the PE-side edge.

---

## Files

| File | Role |
|------|------|
| `login.py` | standalone Kite Connect login (`py -3.14 -m backtest.login`) — writes `.kite_token` without the dashboard |
| `data/fetch.py` | pull + cache Kite historical candles |
| `data/candles.py` | load parquet, resample, serve trailing windows as `Candle` objects |
| `data/synth.py` | toy data generator (plumbing tests only) |
| `marketdata.py` | point-in-time queries (VIX at ts, IV-rank on day, futures price) |
| `option_model.py` | Black-Scholes + VIX-driven IV + IV-crush + theta model |
| `costs.py` | Zerodha F&O brokerage / STT / charges (option vs future asymmetry) |
| `strategy_ds.py` | demand/supply — faithful port of `scheduler._scan_core()` |
| `replay.py` | event-driven day-by-day simulation, multi-instrument pricing |
| `metrics.py` | trade list → scorecard |
| `calibrate.py` | fit the option model to real fills |
| `run.py` | CLI |

## Modelling choices & limitations

- **Entry (`entry_mode`, default `limit`):** a limit order rests at the zone
  proximal and fills only when a later bar's range spans it within
  `signal_expiry_minutes`; if price never returns, no trade. `market` = fill next
  bar open (faithful to current live behaviour). See `replay.py` docstring.
- **Exit:** purely index-path driven — target / stop / time-exit / EOD. The same
  exit is applied to every instrument so P&L differences are the instrument alone.
  Options-side management (`OPTIONS_SL_PCT`, `OPTIONS_TRAIL_PCT`) is **not** layered
  in yet — that's a later mode.
- **Option premiums are modelled, not real** (BS + VIX-driven IV + IV-crush +
  bid/ask half-spread). Entry premiums fit real fills within ~10 pts median, but
  per-trade P&L error is ~₹300–400 and the model runs ~₹150/trade optimistic.
  Treat modelled option P&L as indicative (±₹400). **Futures numbers carry no
  such uncertainty — trust those.**
- **Futures history:** Kite continuous stitching fails for intraday NIFTY FUT, so
  only the front month (~weeks) is real; older history uses the index as the
  proxy (intraday basis ≈ constant, so entry→exit P&L is unaffected).
- **Backtest windows contain only completed bars**, so bar-index expressions
  shifted by one vs `_scan_core` (which sees a forming `candles[-1]`). See the
  note in `strategy_ds.py`.
- One position at a time; re-entry allowed after exit, capped by
  `max_trades_per_day`.
