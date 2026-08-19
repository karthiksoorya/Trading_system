# Trading System — Agent Knowledge Base
**Owner:** Karthikeyan, Chennai  
**Last Updated:** August 2026  
**Purpose:** Complete knowledge to rebuild this system as a pure autonomous agent.

---

## 1. WHAT THIS SYSTEM DOES

Automated intraday Nifty 50 Options trading system:
- Detects Demand/Supply zones using price action (no indicators)
- Scores each zone using a booster system (0–10)
- Sends signals to Telegram for human approval (or auto-executes first trade)
- Places real BUY/SELL limit orders on Zerodha Kite
- Monitors open trades every 1 minute — auto-exits on target, SL, or EOD
- Exports daily CSV and sends EOD summary to Telegram

**Current status (Aug 2026):** Live trading on VPS. Real money. Nifty 50 CE/PE options.

---

## 2. ACTUAL ARCHITECTURE (What Was Built)

```
trading_system/
├── main.py                  ← entry point: --run starts scheduler
├── config.py                ← all constants, load_settings(), save_settings()
├── scheduler.py             ← core loop: scan every 5min, monitor every 1min
├── app.py                   ← Streamlit dashboard (UI for human oversight)
├── notify.py                ← Telegram notifications (signals, approvals, closures)
├── telegram_handler.py      ← polls Telegram callbacks (Approve/Reject/Close buttons)
├── autolearn.py             ← self-learning: disables underperforming zone types
├── backup.py                ← DB backup via Telegram sendDocument
├── brokers/
│   ├── base.py              ← abstract broker interface
│   ├── kite_adapter.py      ← Kite Connect implementation (live orders)
│   └── paper_broker.py      ← paper trading (no real orders)
├── engine/
│   ├── candle.py            ← boring/exciting candle detection
│   ├── zones.py             ← DBR/RBR/RBD/DBD zone detection + state tracking
│   ├── boosters.py          ← scoring: freshness, strength, time, R:R
│   ├── signals.py           ← entry/SL/target calculation
│   ├── confluence.py        ← multi-timeframe agreement check
│   └── position_size.py     ← S.E.T.S risk calculator
├── journal/
│   ├── db.py                ← SQLite: signals table, daily_summary table
│   └── export.py            ← daily CSV export
└── data/
    └── trades.db            ← SQLite database (NEVER commit, always backup)
```

**Key files for agent decisions:**
- `config.py` → `load_settings()` reads live settings (mode, filters, thresholds)
- `journal/db.py` → all DB reads/writes
- `scheduler.py` → the brain of the system
- `notify.py` → all outbound communication

---

## 3. TRADING STRATEGY

### 3.1 Zone Types
| Type | Full Name | Class | Option |
|------|-----------|-------|--------|
| DBR | Drop-Base-Rally | Demand | BUY CE |
| RBR | Rally-Base-Rally | Demand | BUY CE |
| RBD | Rally-Base-Drop | Supply | BUY PE |
| DBD | Drop-Base-Drop | Supply | BUY PE |

**Proximal line** = closest edge of zone to current price (entry trigger)  
**Distal line** = far edge of zone (stop loss placed beyond this)

### 3.2 Booster Scoring (0–10 points)
| Booster | Max | Rule |
|---------|-----|------|
| Freshness | 3 | Fresh zone=3, 1 prior touch=1.5, >1 touch=0 |
| Strength | 2 | Gap/Explosive=2, Strong=1, Weak=0 |
| Time | 2 | 1–3 candles in base=2, 4–6=1, >6=0 |
| R:R | 3 | Overnight≥1:3 AND Intraday≥1:2 = 3pts |
| **Total** | **10** | Min score to trade: 8 (configurable) |

### 3.3 Signal Filters (all must pass)
1. **Zone approach:** LTP within N points of proximal (configurable, default 100)
2. **Zone validity:** No close beyond distal in last 3 candles (zone not broken)
3. **60min trend:** Demand zones only on UP trend, Supply zones only on DOWN trend
4. **Min score:** Booster total ≥ threshold (default 8, currently set lower)
5. **Confluence:** Minimum N timeframes agreeing (currently 1)
6. **Scan window:** Only between configured hours (default 09:15–15:25)
7. **Max trades:** No more than MAX_TRADES_PER_DAY per day
8. **Daily loss limit:** Stop if daily P&L ≤ -MAX_DAILY_LOSS

### 3.4 Entry / Exit Rules
- **Entry:** Proximal line of zone (signal fires when price approaches)
- **Stop Loss:** Beyond distal line + SL_BUFFER_POINTS (default 5pts buffer)
- **Intraday Target:** Entry ± (Entry−SL) × min R:R (1:2 minimum)
- **Options:** ATM strike = round(entry / 50) × 50
- **Expiry:** Next Tuesday (NSE changed Nifty from Thursday → Tuesday, effective 2026)
- **Expiry day rule:** On Tuesday itself, always use NEXT week (never same-day expiry)

### 3.5 Order Details
- **Exchange:** NFO
- **Product:** MIS (intraday — auto-squared at 15:30 by Kite if not closed)
- **Order type:** LIMIT at option LTP ± 2pts rounded to tick 0.05
  - BUY: LTP + 2pts (ensures fill)
  - SELL: LTP − 2pts (ensures fill)
  - Fallback: MARKET if option LTP unavailable
- **Lot size:** 65 units (NSE revised Jan 2026, auto-fetched from Kite instruments)
- **EOD close:** 15:20 (10 min before market close)

---

## 4. LIVE TRADING — HOW IT ACTUALLY WORKS

### 4.1 Daily Morning Routine (before 9:15 AM)
1. Open dashboard → Engine tab
2. Login to Kite (button → browser → TOTP → redirect back)
3. Token auto-saved (valid today only — SEBI requirement, cannot skip)
4. Start Engine → sidebar shows 🟢 Running

### 4.2 Signal Flow
```
Every 5 min (scheduler):
  ↓ fetch Nifty LTP + candles for 3 TFs
  ↓ detect zones on each TF
  ↓ apply all 8 filters above
  ↓ score with boosters
  ↓ if passes all: log to DB + send Telegram
  ↓ user sees: entry, SL, target, score, Approve/Reject buttons

On Approve (Telegram or Dashboard):
  ↓ validate_entry() — check price hasn't moved too far
  ↓ get_options_contract() — find ATM weekly contract
  ↓ place_options_order() — limit BUY on Kite NFO
  ↓ store kite_order_id + options_symbol in DB
  ↓ fetch fill price after 3s → store options_entry_price
  ↓ send trade confirmation to Telegram

Every 1 min (scheduler monitors open trades):
  ↓ fetch Nifty LTP
  ↓ if demand: LTP >= target → SELL (target) | LTP <= SL → SELL (stoploss)
  ↓ if supply: LTP <= target → SELL (target) | LTP >= SL → SELL (stoploss)
  ↓ on exit: place SELL order → fetch fill price → update DB → notify Telegram

15:20 EOD:
  ↓ close all open trades at current LTP
  ↓ export daily CSV
  ↓ send EOD summary to Telegram
```

### 4.3 Auto-Trade First Signal (optional)
Setting: `AUTO_FIRST_TRADE = True/False`  
When ON: first qualifying signal of the day is auto-approved and ordered without human input.  
Subsequent signals always require manual approval.  
Fallback: if token not loaded or order fails → sends normal Telegram notification instead.

### 4.4 Kite Token (Critical)
- Token file: `data/kite_token.json` (gitignored)
- Valid: today only (resets at midnight)
- If missing/expired: engine scans but all live orders fail
- IPv4 + IPv6 of VPS must both be whitelisted in Kite developer console

---

## 5. WHAT EACH SETTING DOES

| Setting | Default | Effect |
|---------|---------|--------|
| MODE | paper/live | live = real Kite orders; paper = no orders |
| ENTRY_TIMEFRAME | 5minute | TF that generates signals |
| SCAN_TIMEFRAMES | all 3 | TFs used for confluence |
| SCAN_ZONE_CLASSES | demand | demand=CE, supply=PE |
| MIN_BOOSTER_SCORE | 8 | Min score to signal |
| MIN_CONFLUENCE | 1 | Min TFs agreeing |
| ZONE_APPROACH_POINTS | 100 | Max distance from zone to fire signal |
| SL_BUFFER_POINTS | 5 | Buffer beyond distal for SL |
| SIGNAL_EXPIRY_MINUTES | 45 | Auto-expire pending signals |
| SCAN_WINDOW | 09:15–15:25 | Active scan hours |
| MAX_TRADES_PER_DAY | 3 | Hard limit on trades |
| MAX_DAILY_LOSS | 500 | Stop trading after this loss (points) |
| AUTO_FIRST_TRADE | False | Auto-execute first signal of day |

---

## 6. DATABASE SCHEMA

**signals table** (one row per signal):
```
id, status (pending/approved/rejected/expired/closed)
date, time_signal, zone_type, zone_class, timeframe
proximal, distal, entry, stop_loss, intraday_target, overnight_target
booster_score, freshness, strength, time_score, rr_score
entry_type, position_size, confluence_count, confluence_tfs
exit_time, exit_price, exit_reason (target/stoploss/manual/eod)
pnl_points, result (win/loss/breakeven), notes, mode (paper/live)
kite_order_id, options_symbol, options_lot_size
options_entry_price, options_exit_price, options_exit_order_id
```

**Key DB functions:**
- `log_signal()` → insert new signal
- `approve_signal()` → status = approved
- `reject_signal()` → status = rejected
- `close_trade()` → status = closed, calculates pnl_points
- `get_open_trades()` → status = approved (active positions)
- `zone_signaled_today()` → prevents duplicate signals (excludes rejected)
- `update_signal_order()` → stores kite_order_id + options_symbol after BUY
- `update_signal_entry_price()` → stores actual fill price after BUY
- `update_signal_exit_order()` → stores SELL order_id + fill price

---

## 7. INFRASTRUCTURE

**VPS:** AWS Lightsail, Mumbai region  
**IP:** 13.201.210.4 (IPv4) + 2406:da1a:1fd0:3a00:af7e:69a5:c28d:3ea2 (IPv6)  
**Both IPs must be in Kite developer console whitelist.**

**Service:** systemd (`sudo systemctl start/stop/restart trading`)  
**Port:** 8501 (Streamlit dashboard, public)  
**Dashboard URL:** http://13.201.210.4:8501

**Key VPS commands:**
```bash
sudo systemctl status trading       # is engine running?
sudo journalctl -u trading -f       # live logs
sudo journalctl -u trading --since today | grep -E "SIGNAL|Skipped|ERROR"
git pull && sudo systemctl restart trading   # deploy update
cp ~/Trading_system/data/trades.db ~/trades_backup_$(date +%Y%m%d).db  # backup
```

**Git tags:**
- `v1.0-stable` (commit a09340b, Aug 14 2026) — first successful live trading day

---

## 8. LESSONS LEARNED FROM REAL TRADES

### Aug 17, 2026 — First live day ✅ (3/3 target hits)
- Strong UP trending day → demand zones (CE) worked perfectly
- +73, +72.5, +82.4 Nifty index points per trade
- Actual options P&L was only ₹29 because user closed manually on Kite within seconds
- **Rule: NEVER close manually on Kite — always use Telegram "Early Exit" or dashboard**

### Aug 18, 2026 — Expiry Tuesday (no signals)
- Zones detected but all violated by expiry day volatility
- **Rule: Expiry Tuesdays = low signal quality, expect fewer/no signals**

### Aug 19, 2026 — Supply zones enabled (loss ❌)
- System worked correctly — placed PE on a supply zone signal
- Nifty bounced UP from supply zone → PE lost value → manual exit at loss
- **Rule: Supply zones only work on STRONG downtrend days (not ranging/weak)**
- **Rule: Never enable a new feature (supply zones) on a live day without paper testing first**
- User rejected all signals due to phone call — correct decision
- **Rule: If you can't focus for 30 minutes, reject all signals**

### Patterns confirmed:
- Best for CE trades: Strong UP trending 60min day
- Best for PE trades: Strong DOWN trending 60min day
- Avoid: Expiry Tuesdays, ranging days, distracted sessions
- Options P&L ≠ Nifty index P&L (delta ~0.5, theta decay, IV changes)
- Manual Kite close while DB thinks trade is open → system places second SELL → naked short

---

## 9. BUGS FIXED AND WHY

| Bug | Root Cause | Fix |
|-----|-----------|-----|
| Contract not found | `instruments()` returns `datetime` not `date` — `datetime == date` is always False | `_norm_expiry()` helper normalises type |
| Wrong expiry day | Code hardcoded Thursday (weekday=3), NSE changed to Tuesday (weekday=1) | `(1 - today.weekday()) % 7` |
| Same-day expiry risk | On Tuesday, days_ahead=0 | `if days_ahead == 0: days_ahead = 7` |
| Order failed but signal still open | Exception left signal as 'approved' forever | catch exception → `reject_signal()` |
| Telegram button spinner forever | `return` in handler skipped `answerCallbackQuery` | Use `_order_failed` flag, never return early |
| Market order slippage | Wide bid-ask on options | Limit orders at LTP ± 2pts, tick-rounded |
| Wrong strike | Used current LTP for strike, not signal entry price | Use `row["entry"]` for `get_options_contract()` |
| EOD not placing SELL | `_live_exit()` was nested inside `monitor_open_trades()` — `end_of_day()` couldn't call it | Promoted to module-level function |
| Auto-trade pre-approves too early | `approve_signal()` called before order placement — if fails, signal stuck in 'approved' | Move `approve_signal()` to after successful order |
| Double SELL on manual Kite close | User closes on Kite, system doesn't know, scheduler also fires SELL → naked short | Don't close manually on Kite — use system buttons only |

---

## 10. HOW TO REBUILD AS A PURE AGENT

### 10.1 What a pure agent would do differently
Currently: human approves each signal via Telegram  
Future agent: agent decides to approve or reject based on rules

**Agent decision rules:**
1. Check 60min trend direction — match to zone class (demand=up, supply=down)
2. Check booster score ≥ threshold
3. Check no open trade already exists
4. Check entry price hasn't moved too far (validate_entry tolerance)
5. Check it's not expiry day Tuesday (or use next week's contract)
6. Check daily P&L not already at loss limit
7. If all pass → approve and place order automatically

**Agent exit rules:**
- Target hit (Nifty index) → SELL immediately
- SL hit (Nifty index) → SELL immediately
- Trade open > 2 hours with no movement → consider manual exit
- 15:20 → SELL everything (EOD)

**Agent risk rules:**
- Max 1 open trade at a time
- Max 3 trades per day
- Stop trading after ₹500 loss in a day
- Never trade in first 30 minutes (09:15–09:45) — high volatility
- Never trade last 30 minutes (15:00–15:30) — liquidity risk

### 10.2 What the agent needs to know at startup each day
1. Is Kite token valid for today? (check `data/kite_token.json`)
2. What is current mode? (paper/live)
3. How many trades taken today? (DB query)
4. What is current daily P&L? (DB query)
5. Is there an open position? (DB + Kite positions API)

### 10.3 What an agent must NEVER do
- Place order without valid Kite token
- Place SELL without verifying open position exists on Kite
- Override the 60min trend filter (it prevents counter-trend losses)
- Trade on expiry Tuesday with same-day contract
- Approve more than 1 signal while a trade is open
- Place market orders (always limit — slippage on options is severe)

### 10.4 Minimum viable agent (next step)
The system already has AUTO_FIRST_TRADE. To make it fully autonomous:
1. Remove human approval requirement for all signals (not just first)
2. Add agent confidence check (trend + score + confluence threshold)
3. Add position verification (check Kite positions before placing SELL)
4. Add daily journal summary with reasoning for each decision

---

## 11. ENVIRONMENT / CREDENTIALS

**Stored in `.env` (gitignored — NEVER commit):**
```
KITE_API_KEY=...
KITE_API_SECRET=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

**Stored in `data/` (gitignored):**
```
kite_token.json     ← daily Kite access token
trades.db           ← SQLite database (backup before any destructive operation)
settings.json       ← live settings (mode, filters, thresholds)
```

**To restore from stable version:**
```bash
git fetch --tags
git checkout v1.0-stable
sudo systemctl restart trading.service
```

---

## 12. OPEN QUESTIONS FOR FUTURE

- Should SL/target track options premium price instead of Nifty index price?
- Add Bank Nifty as second instrument?
- Enable trailing SL (move to breakeven at 1:1 R:R)?
- Full agent mode — remove all human approvals?
- Position verification before SELL (check Kite positions API to avoid naked short)?
- Telegram OTP login gate — re-enable when stable?

---

*This document is the single source of truth for rebuilding this system.*  
*Update after every significant trading day or code change.*
