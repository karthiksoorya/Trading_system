# Nifty Trading System — Project Context
**Owner:** Karthikeyan, Chennai  
**Last Updated:** June 2026  
**Purpose:** Upload this file to a new Claude session (Project Knowledge or first message) to resume development with full context.

---

## 1. WHAT THIS IS

An automated Nifty 50 Options paper trading system built on Zerodha/Kite Connect API.  
Strategy: Pure price action — Demand & Supply zones with booster scoring.  
Current phase: **Paper trading** (validating strategy before live execution).

- **Broker:** Zerodha (Kite Connect API — ₹500/month)
- **Instrument:** Nifty 50 Options (NSE)
- **Capital:** ₹10,000/month allocated
- **Max risk:** 1% per day = ₹100 | Max 4 trades/day | Min score 8/10 to trade

---

## 2. HOW TO RUN

```bash
# Every morning before market (generates daily Kite access token)
python main.py --token

# Start the dashboard
streamlit run app.py
# Laptop: http://localhost:8501
# VPS:    http://13.201.210.4:8501

# CLI alternatives
python main.py           # interactive menu
python main.py --run     # start engine directly
python main.py --scan    # one-time test scan
python main.py --status  # today's P&L
```

---

## 3. FOLDER STRUCTURE

```
trading_system/
├── config.py                  ← all settings + settings.json loader
├── .env                       ← KITE_API_KEY, KITE_API_SECRET (never commit)
├── .kite_token                ← daily access token (auto-generated, never commit)
├── .engine.pid                ← engine process ID (auto-managed)
├── app.py                     ← Streamlit dashboard (main UI)
├── main.py                    ← CLI entry point
├── scheduler.py               ← engine loop, zone scanning, trade monitoring
├── brokers/
│   ├── base.py                ← abstract broker interface (Candle, Quote, BrokerBase)
│   ├── __init__.py            ← get_broker() factory
│   ├── kite_adapter.py        ← Kite Connect implementation
│   └── upstox_adapter.py      ← stub (future)
├── engine/
│   ├── candle.py              ← boring/exciting candle detection
│   ├── zones.py               ← DBR/RBR/RBD/DBD zone detection
│   ├── boosters.py            ← freshness/strength/time/RR scoring
│   ├── signals.py             ← signal generation with SL buffer
│   ├── confluence.py          ← multi-timeframe confluence check
│   └── position_size.py       ← S.E.T.S position sizing
├── journal/
│   ├── db.py                  ← SQLite handler (all DB operations)
│   └── export.py              ← CSV export
├── data/
│   ├── trades.db              ← SQLite database (auto-created)
│   ├── settings.json          ← user-saved UI settings (auto-created)
│   └── exports/               ← daily CSV exports
└── docs/
    ├── PROJECT_CONTEXT.md     ← this file
    ├── help.txt               ← setup guide
    └── credentials.txt        ← placeholder only (real keys in .env)
```

---

## 4. STRATEGY RULES

### Candle Classification
- **Boring:** body < 50% of range → base candle
- **Exciting:** body > 50% of range → leg in / leg out

### Zone Patterns
| Pattern | Class | Structure |
|---|---|---|
| DBR | Demand | Drop → Base → Rally |
| RBR | Demand | Rally → Base → Rally |
| RBD | Supply | Rally → Base → Drop |
| DBD | Supply | Drop → Base → Drop |

- **Proximal line** = closest edge to current price (entry)
- **Distal line** = farthest edge (stop loss base)

### Booster Scoring (max 10 pts, min 8 to trade)
| Booster | Max | Rules |
|---|---|---|
| Freshness | 3 | Fresh=3, 1 touch=1.5, >1=0 |
| Strength | 2 | Gap/Explosive=2, Strong=1, Weak=0 |
| Time | 2 | 1-3 candles=2, 4-6=1, >6=0 |
| R:R | 3 | ON≥1:3 & ID≥1:2=3, ON≥1:2 & ID≥1:1.5=1.5, else=0 |

### Entry Types
- **Type 1** (score 10): Limit at proximal — can be automated
- **Type 2** (score 8-9): Anywhere in zone
- **Type 3**: Confirmation candle — manual

### Position Sizing (S.E.T.S)
```
Risk/trade = ₹100 / remaining_trades_today
Position size = Risk / (entry - stop_loss in points)
Stop loss = distal ± SL_BUFFER_POINTS (default 5, configurable in UI)
```

### Multi-Timeframe Confluence
- 3 TFs scanned: 5min (entry), 15min (trend), 60min (curve)
- Signal must have zone overlap with at least one higher TF

---

## 5. FULL FEATURE LIST (BUILT)

### Engine
- [x] Zone detection across 3 timeframes
- [x] Booster scoring
- [x] Multi-timeframe confluence
- [x] Scans every 5 min from 10:05 AM
- [x] Market hours check (weekday + 09:15–15:30)
- [x] Weekend detection → uses last trading day's data
- [x] Max trades/day and daily loss limit guards

### Approval Flow
- [x] Signals saved as `status = pending`
- [x] Only today's signals shown for approval
- [x] Stale signals (previous days) auto-rejected on app startup
- [x] Only ONE active trade allowed at a time
- [x] Approve → status = approved → engine monitors
- [x] Reject → status = rejected (kept for analysis)
- [x] Bulk "Reject All Pending" button

### Auto-Exit (runs every 1 min while engine is running)
- [x] Demand zone: exit at target if LTP ≥ target | exit at SL if LTP ≤ SL
- [x] Supply zone: exit at target if LTP ≤ target | exit at SL if LTP ≥ SL
- [x] EOD auto-close at 15:20 for any open trades
- [x] P&L calculated and logged on close

### Dashboard (Streamlit — app.py)
- [x] **Engine tab**: token generation, start/stop engine, scan now, status, settings
- [x] **Approvals tab**: open trades with live P&L, pending signal cards with approve/reject
- [x] **Signals tab**: trade log by date, manual close form, CSV export
- [x] **Performance tab**: cumulative P&L chart, daily P&L bar chart, zone/TF breakdown, validation checklist
- [x] Sidebar: engine status, pending count, trades today, daily P&L, refresh
- [x] SL buffer slider (0–30 pts, saved to settings.json)
- [x] Session state for engine start/stop buttons (no crash on click)

### Infrastructure
- [x] SQLite with auto-migration for new columns
- [x] CSV export per day
- [x] PID file for engine process management
- [x] Windows-compatible process check (tasklist)
- [x] .env for credentials (never committed)
- [x] settings.json for user-configurable values

---

## 6. DATABASE SCHEMA

### signals table
```
id, status (pending/approved/rejected), date, time_signal,
zone_type, zone_class, timeframe, proximal, distal,
entry, stop_loss, intraday_target, overnight_target,
booster_score, freshness, strength, time_score, rr_score,
entry_type, position_size, confluence_count, confluence_tfs,
exit_time, exit_price, exit_reason (target/stoploss/manual/eod),
pnl_points, result (win/loss/breakeven), rule_based, notes, mode
```

### daily_summary table
```
id, date, trades_taken, wins, losses, total_pnl, max_daily_loss, notes
```

---

## 7. KEY CONFIG VALUES (config.py)

```python
MODE              = "paper"      # "paper" | "live"
BROKER            = "kite"
CAPITAL           = 10_000       # ₹
MAX_RISK_PCT      = 0.01         # 1% → ₹100/day
MAX_TRADES_PER_DAY = 4
MIN_BOOSTER_SCORE  = 8
SL_BUFFER_POINTS   = 5           # overridden by data/settings.json via UI
TF_LOWER          = "5minute"
TF_INTERMEDIATE   = "15minute"
TF_HIGHER         = "60minute"
SCAN_START        = "10:05"
MARKET_CLOSE      = "15:30"
```

---

## 8. DEPLOYMENT

- **Local:** Windows laptop, run `streamlit run app.py`
- **VPS:** 13.201.210.4 (Linux), same command, accessible from phone browser
- **Git:** Private GitHub repo — `.env`, `.kite_token`, `data/` are gitignored
- **Kite credentials:** Stored in `.env` as `KITE_API_KEY` and `KITE_API_SECRET`
- **Daily token:** Must be generated every morning via Engine tab → Step 1

---

## 9. WHAT IS NOT BUILT YET (NEXT STEPS)

### High Priority
- [ ] **Telegram notifications** — notify on new signal, trade closed, EOD summary
  - Simple: `pip install requests`, add `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` to `.env`
  - Notify on: signal detected, approved, auto-exit (target/SL), EOD summary
- [ ] **Live order placement** — `place_order()` in KiteAdapter
  - Needs: strike price selection (CE for demand, PE for supply), expiry lookup
  - Gate behind `config.MODE == "live"`
  - Only after validation checklist is complete

### Medium Priority
- [ ] **VPS auto-start** — systemd service so engine restarts on VPS reboot
- [ ] **Streamlit auto-refresh** — page refreshes every 30s without clicking button
- [ ] **Kite login automation** — headless browser (Playwright) for token on VPS

### Low Priority
- [ ] **Bank Nifty support** — add symbol to config, same logic applies
- [ ] **Upstox adapter** — implement when Upstox API is enabled

---

## 10. VALIDATION CHECKLIST (before going live)

- [ ] 20+ paper trades logged
- [ ] Win rate > 50%
- [ ] Avg P&L positive
- [ ] System detects zones correctly (manually verified 5+ times)
- [ ] No crashes for 5 consecutive days

Progress visible in Performance tab of dashboard.

---

## 11. KNOWN DECISIONS & REASONS

| Decision | Reason |
|---|---|
| SQLite not PostgreSQL | Sufficient for paper trading, zero setup |
| Streamlit not React | Fastest to build, works on phone browser via VPS |
| Paper mode first | Master doc rule — 20 validated trades before live |
| One active trade at a time | Capital is small (₹10K), can't manage multiple positions |
| SL at distal line | Zone invalidation point — pure price action rule |
| SL buffer 5 pts default | Avoid wick-triggered stops without moving far from zone |
| Tasklist for PID check on Windows | os.kill(pid, 0) unreliable on Windows |
| session_state for button state | st.rerun() alone caused button disable/crash issues |
| Today-only approvals | Yesterday's zones may be invalid, no point approving stale signals |

---

*Upload this file to Claude Project Knowledge or paste at start of new session to resume development.*
