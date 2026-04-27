# Trading System — Master Context Document
**Owner:** Karthikeyan | **Last Updated:** April 2026  
**Purpose:** Context document for AI-assisted development. Upload to Project Knowledge.

---

## 1. WHO & WHY

- **Trader:** Karthikeyan, Chennai
- **Broker:** Zerodha (Kite platform)
- **Instrument:** Nifty 50 Options (NSE)
- **Capital:** ₹10,000/month allocated to trading
- **Goal:** Build options trading into a reliable income stream
- **Core motivation:** Eliminate emotional decisions through automation
- **Current status:** Paper trading phase — validating strategy before live execution

---

## 2. FINANCIAL CONTEXT

- Monthly income: ₹1,50,000
- Monthly deficit: ₹-54,796 (spends exceed income)
- EMI burden: ₹89,992/month (60% of income)
- Trading capital: ₹10,000/month to Zerodha
- **Implication:** Capital preservation is critical — no room to absorb large losses

---

## 3. TRADING STRATEGY (From PDFs)

### 3.1 Core Philosophy
- Pure Price Action — no indicators, no black box
- Demand & Supply zones — institutional order flow
- Day trading Nifty for income generation
- Wait till 10:00 AM before any action

### 3.2 Candle Classification (PDF 1)
- **Boring candle:** Body < 50% of total range → balance, accumulation
- **Exciting candle:** Body > 50% of total range → imbalance, directional move
- Leg In and Leg Out must always be Exciting candles
- Base must always be Boring candles

### 3.3 Zone Detection (PDF 2)

**Demand Zones:**
- DBR (Drop Base Rally) → Proximal = Highest body in base, Distal = Lowest low
- RBR (Rally Base Rally) → Proximal = Highest body in base, Distal = Lowest low (excluding leg in)

**Supply Zones:**
- RBD (Rally Base Drop) → Proximal = Lowest body in base, Distal = Highest high
- DBD (Drop Base Drop) → Proximal = Lowest body in base, Distal = Highest high (excluding leg in)

**Zone Lines:**
- Proximal Line = closest to current market price
- Distal Line = farthest from current market price

**Multi-timeframe:** Minimum 3 timeframes
- Higher TF → Demand/Supply curve
- Intermediate TF → Trend
- Lower TF → Entry

### 3.4 Booster Scoring System (PDF 3)

| Booster | Max Score | Rules |
|---|---|---|
| Freshness | 3 | Fresh=3, 1 touch=1.5, >1 touch=0 |
| Strength | 2 | Gap/Explosive=2, Strong=1, Weak=0 |
| Time | 2 | 1-3 candles=2, 4-6=1, >6=0 |
| R:R | 3 | ON≥1:3 & ID≥1:2=3, ON≥1:2 & ID≥1:1.5=1.5, else=0 |
| **Total** | **10** | |

**Entry Rules based on Score:**
- Score 0-7 → DO NOT TRADE
- Score 8-9 → Entry Type 2 or 3
- Score 10 → Entry Type 1 (Limit entry, set and forget)

### 3.5 Entry Types (PDF 3)
- **Type 1 (Limit):** Set at proximal line, highest probability, can be automated
- **Type 2 (Zone):** Enter anywhere in zone, lowest risk, highest reward
- **Type 3 (Confirmation):** Wait for confirmation candle, cannot be automated

### 3.6 Position Sizing — S.E.T.S (PDF 4)
```
Stop → Entry → Target → Size

Max Risk/Day = Capital × 1% = ₹100 (on ₹10K)
Risk/Trade = Max Risk/Day ÷ Number of trades (max 4)
Position Size = Risk per Trade ÷ (Entry - SL)
```

### 3.7 Trade Management (PDF 3)
- At 1:1 R:R → Move SL to breakeven (entry price)
- Intraday target minimum: 1:2 R:R
- Overnight target minimum: 1:3 R:R
- Trailing SL: manual trailing gives best results

### 3.8 Daily Routine (Learnings PDF)
```
Before 10:00 AM:
  → Mark Previous Day High/Low
  → Mark Current Day High/Low
  → Opening Low = Bullish bias
  → Opening High = Bearish bias

10:05 AM:
  → Scan for zones
  → Score boosters
  → Log signals with score ≥ 8

During session:
  → Watch proximal line touches
  → Note VWAP
  → Observe pullback strength (Strong/Weak, Deep/Light curve)

Entry habit to fix:
  → Change from Market price to Manual/Limit entry
  → Always apply Stop Loss (use Bracket Order)
```

---

## 4. TECH STACK DECIDED

| Component | Choice | Reason |
|---|---|---|
| Language | Python 3.10+ | Best ecosystem for trading |
| Live Data | Kite Connect API (₹500/month) | Real-time WebSocket, covers all needs |
| Backup Broker | Upstox API (future) | Account exists, API not enabled yet |
| Storage | SQLite | Local, zero cost, sufficient for paper trading |
| Export | CSV | Easy to review on phone/Excel |
| Scheduler | Python schedule library | Simple cron-like, no overhead |
| Future UI | React (mobile-first) | Karthikeyan primarily on mobile |
| Future API | FastAPI | Lightweight, async-friendly |

---

## 5. ARCHITECTURE DECISIONS

### 5.1 Phase 1 (NOW — Build This)
**Lean script. No UI. No API. Just validate the strategy.**

```
Python script
    ↓
Kite WebSocket (live ticks)
    ↓
Zone detection + Booster scoring
    ↓
Score ≥ 8 → Log to CSV + SQLite
    ↓
Review every evening
```

### 5.2 Broker Adapter Pattern (Build from Day 1)
Even in lean version, broker is abstracted:

```python
# brokers/base.py — abstract interface
class BrokerBase:
    def get_ltp(self, symbol): pass
    def get_ohlc(self, symbol, interval): pass
    def get_depth(self, symbol): pass
    def get_historical(self, symbol, interval, days): pass

# brokers/kite_adapter.py — Kite implementation
class KiteAdapter(BrokerBase): ...

# brokers/upstox_adapter.py — future
class UpstoxAdapter(BrokerBase): ...

# config — one line switch
BROKER = "kite"  # change to "upstox" anytime
```

### 5.3 Folder Structure (Lean Version)
```
trading_system/
├── config.py                  ← broker, mode, capital, risk params
├── brokers/
│   ├── base.py                ← abstract interface
│   ├── kite_adapter.py        ← Kite implementation
│   └── upstox_adapter.py      ← placeholder for future
├── engine/
│   ├── candle.py              ← boring/exciting detection
│   ├── zones.py               ← DBR/RBR/RBD/DBD detection
│   ├── boosters.py            ← scoring system
│   ├── signals.py             ← entry/SL/target generator
│   └── position_size.py       ← S.E.T.S calculator
├── journal/
│   ├── db.py                  ← SQLite handler
│   └── export.py              ← CSV export
├── scheduler.py               ← runs at 10:05 AM
└── main.py                    ← entry point
```

### 5.4 Paper Trade CSV Schema
```
Date, Time_Signal, Zone_Type, Proximal, Distal, Entry, SL, Target,
Booster_Score, Freshness, Strength, Time_Score, RR_Score,
Entry_Type, Position_Size, Exit_Time, Exit_Price, Exit_Reason,
PnL_Points, Result, Rule_Based, Notes
```

### 5.5 Future Phase 2 (Only after 20+ validated trades)
- FastAPI backend for config management
- React mobile-first UI dashboard
- All config in DB — no code changes for any setting
- Engine state machine (STARTING→SCANNING→MONITORING→TRADING→PAUSED→STOPPED)
- Pause/Resume without losing open trade monitoring
- Engine state persisted to DB for crash recovery
- Notifications (Telegram preferred)
- Live mode toggle with confirmation step

---

## 6. KITE CONNECT API — DATA COVERAGE

| Data Needed | Kite API Call | Available |
|---|---|---|
| 5-min OHLC candles | historical_data() | ✅ |
| Previous day H/L | historical_data() | ✅ |
| Live spot price | ltp() | ✅ |
| Live WebSocket ticks | KiteTicker | ✅ |
| Market depth (Strength booster) | quote() → depth | ✅ |
| VIX live | ltp("NSE:INDIA VIX") | ✅ |

**Note:** Kite requires new access token every day via login flow.
Can be semi-automated using headless browser script.

**Pricing:**
- Personal API: Free (orders only, no market data)
- Connect API: ₹500/month (real-time + historical data)
- Order placement: Free since March 2025

---

## 7. KEY RULES — NON NEGOTIABLE

- Score < 8 → no trade logged at all
- Max 4 trades per day
- Max daily loss = 1% of capital = ₹100
- Paper mode until 20+ trades validated
- No manual override in automation (defeats the purpose)
- Entry must be limit/bracket order — never market price
- Always set stop loss before entry

---

## 8. VALIDATION CHECKLIST (Before Going Live)

- [ ] 20+ paper trades logged
- [ ] Win rate > 50%
- [ ] Average R:R > 1:2
- [ ] Rule-based trades outperform instinct trades
- [ ] System detects zones correctly (manually verified 5+ times)
- [ ] Booster scoring matches manual calculation
- [ ] No crashes during market hours for 5 consecutive days

---

## 9. DEPLOYMENT ARCHITECTURE

### 9.1 Phase 1 — Local Laptop (Now)
```
Your Laptop (Windows/Mac)
├── Python script running
├── SQLite DB stored locally
├── CSV exported to local folder
└── Kite WebSocket connected
```
**Pros:** Free, zero setup, start immediately
**Cons:** Laptop must be ON during market hours (9:15–15:30)
**Verdict:** Fine for paper trading validation

---

### 9.2 Phase 2 — VPS Cloud Server (When going live)

```
VPS (Cloud Server — Always ON)
├── Ubuntu 22.04
├── Python trading engine (runs as service)
├── FastAPI backend
├── SQLite / PostgreSQL DB
├── Nginx (reverse proxy)
└── React UI (served as static files)
         ↕
   Your Mobile Browser
   (access dashboard anywhere)
```

**Why VPS over laptop:**
- Always ON — market hours covered even if you're away
- No dependency on your internet/power at home
- Access UI from phone anywhere
- ₹200–500/month cost

**Recommended VPS options for India:**

| Provider | Plan | Cost | Best For |
|---|---|---|---|
| Hetzner | CX22 (2vCPU, 4GB) | ~₹400/month | Best value |
| DigitalOcean | Basic Droplet | ~₹850/month | Easy setup |
| AWS Lightsail | $5 plan | ~₹420/month | Reliable |
| Hostinger VPS | KVM 1 | ~₹200/month | Cheapest |

**Recommendation:** Hetzner or Hostinger for cost, given your budget constraints.

---

### 9.3 Deployment Stack on VPS

```
Layer               Tool
─────────────────────────────────
OS                  Ubuntu 22.04 LTS
Process Manager     PM2 or systemd
Python Engine       Runs as systemd service
Web Server          Nginx
SSL Certificate     Let's Encrypt (free)
Domain              Optional (use IP directly)
DB                  SQLite (Phase 1) → PostgreSQL (Phase 2)
Logs                /var/log/trading/
Backups             CSV auto-export daily to Google Drive
```

---

### 9.4 How Daily Token Refresh Works on VPS

Kite needs a new access token every day — this is the trickiest part on a server:

**Option A — Semi-auto (Recommended for Phase 1)**
```
6:00 PM daily → Kite sends login link to your email/Telegram
You click, login on phone (30 seconds)
Token auto-saved to DB
Engine uses it next morning
```

**Option B — Fully automated (Phase 2)**
```
Headless browser (Playwright) on VPS
Runs at 8:30 AM daily
Logs into Kite automatically
Saves token to DB
Engine starts at 10:05 AM with fresh token
```

---

### 9.5 Data Backup Strategy

```
SQLite DB → Auto backup to Google Drive daily (rclone)
CSV trades → Google Drive sync every evening
Config DB → Backed up before any change
Logs → Retained for 30 days
```

---

### 9.6 Deployment Checklist (When Ready for VPS)

- [ ] VPS provider chosen and server created
- [ ] Ubuntu 22.04 setup + Python installed
- [ ] Code pushed to GitHub (private repo)
- [ ] VPS pulls from GitHub
- [ ] systemd service created for trading engine
- [ ] Nginx configured for UI + API
- [ ] SSL certificate installed
- [ ] Token refresh flow tested
- [ ] Daily backup to Google Drive verified
- [ ] Mobile browser access to dashboard confirmed

---

### 9.7 Phase Summary

```
Phase 1 — Paper Trading
  Deployment: Local laptop
  Cost: ₹500/month (Kite API only)
  Goal: Validate strategy with 20+ trades

Phase 2 — Live Trading
  Deployment: VPS (Hetzner ~₹400/month)
  Cost: ₹900/month (Kite + VPS)
  Goal: Automated live execution with UI
  
Phase 3 — Scale
  Add Bank Nifty, multiple strategies
  PostgreSQL for larger data
  Multi-user if sharing signals
```

---

## 10. OPEN QUESTIONS / FUTURE DECISIONS



- Telegram notification integration (preferred channel?)
- VPS deployment vs local laptop (₹200-500/month VPS option)
- Upstox API enablement timeline
- Whether to add Bank Nifty alongside Nifty 50
- Claude Desktop + MCP server for live data (deferred, revisit later)

---

## 11. BUILD SEQUENCE

```
Session 1 (Next laptop session):
  → config.py + brokers/base.py + kite_adapter.py

Session 2:
  → engine/candle.py + zones.py

Session 3:
  → engine/boosters.py + signals.py + position_size.py

Session 4:
  → journal/db.py + export.py + scheduler.py + main.py

Session 5:
  → End-to-end test with paper mode
  → First real paper trade logged
```

---

---

## 12. CREDENTIALS & SETUP

### 12.1 Kite Connect API
- API Key and Secret stored in `.env` file in the project root
- Credentials are in `docs/credentials.txt` (plain text, local only)
- `.env` is loaded automatically by `config.py` via `python-dotenv`

### 12.2 Daily Token Refresh
```
Every morning before market open:
  python main.py --token
  → Opens browser login URL
  → Paste request_token from redirect URL
  → Access token saved to .kite_token (valid for that day only)
```

### 12.3 Security Warning
- `.env` and `docs/credentials.txt` contain real API credentials
- This project is inside OneDrive — these files ARE syncing to the cloud
- Recommended: move the project outside OneDrive, or exclude the folder from OneDrive sync
- Never commit `.env` or `credentials.txt` to GitHub

### 12.4 First Run Checklist
```
1. pip install kiteconnect schedule pandas python-dotenv
2. Verify .env has correct KITE_API_KEY and KITE_API_SECRET
3. python main.py --token   (once every morning)
4. python main.py           (starts engine, waits for 10:05 AM)
```

---

*Upload this file to Project Knowledge to retain full context across all future sessions.*
