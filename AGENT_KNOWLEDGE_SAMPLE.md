# Trading System — Setup & Overview
**Last Updated:** August 2026  
**Purpose:** Minimal guide for anyone setting up or exploring this system.

---

## What This System Does

Automated intraday Nifty 50 Options trading based on pure price action (demand/supply zones):
- Scans for Demand/Supply zones every 5 minutes
- Scores each zone (0–10 booster system)
- Sends signals to Telegram — you approve or reject
- Places limit orders on Zerodha Kite automatically on approval
- Monitors open trades every 1 minute — auto-exits on target, SL, or EOD (15:20)
- Sends EOD summary to Telegram

**Default mode: Paper trading (no real orders until you switch to Live)**

---

## Prerequisites

- Python 3.10+
- Zerodha account with [Kite Connect API](https://kite.trade/) (₹500/month)
- Telegram bot — create one via [@BotFather](https://t.me/BotFather)
- A VPS or always-on machine for live trading (local laptop works for paper mode)

---

## Setup

```bash
git clone https://github.com/YOUR_USERNAME/trading_system.git
cd trading_system
pip install -r requirements.txt
```

Create `.env` in project root:
```
KITE_API_KEY=your_api_key
KITE_API_SECRET=your_api_secret
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

Run dashboard:
```bash
streamlit run app.py
```

---

## Daily Routine

1. Open dashboard → Engine tab
2. Click **Login to Kite** (required every day — Kite token expires at midnight)
3. Click **Start Engine**
4. Signals arrive on Telegram with Approve / Reject buttons

---

## Key Settings

| Setting | Default | What it does |
|---------|---------|-------------|
| MODE | paper | `paper` = no real orders, `live` = real Kite orders |
| SCAN_ZONE_CLASSES | demand | `demand` = CE options, `supply` = PE options |
| MIN_BOOSTER_SCORE | 8 | Minimum zone quality score to generate a signal |
| MAX_TRADES_PER_DAY | 3 | Hard daily trade limit |
| ZONE_APPROACH_POINTS | 100 | How close price must be to a zone to signal |
| AUTO_FIRST_TRADE | False | Auto-approve first signal of the day |

All settings are configurable from the dashboard — no code changes needed.

---

## Architecture

```
scheduler.py    ← core loop (scan + monitor)
engine/         ← zone detection, scoring, signal generation
brokers/        ← Kite Connect (live) or paper broker
journal/        ← SQLite database + CSV export
app.py          ← Streamlit dashboard
notify.py       ← Telegram notifications
telegram_handler.py  ← Approve/Reject/Close button handler
```

---

## Security Notes

These files contain credentials — **never commit them:**
```
.env
data/kite_token.json
data/trades.db
data/settings.json
```

All are in `.gitignore` by default.

**Kite token** is valid for one day only. Renew each morning via the dashboard Login button.

If deploying to a VPS, whitelist both IPv4 and IPv6 of your server in the [Kite developer console](https://developers.kite.trade/).

---

## VPS Deployment (for live trading)

```bash
# Check status
sudo systemctl status trading

# View live logs
sudo journalctl -u trading -f

# Deploy an update
git pull && sudo systemctl restart trading

# Backup database before any risky operation
cp ~/trading_system/data/trades.db ~/trades_backup_$(date +%Y%m%d).db
```

---

## Stable Versions

| Tag | Description |
|-----|-------------|
| `v1.0-stable` | First successful live trading day (Aug 2026) |
| `v3.0-pre-training` | Agent knowledge base added |

```bash
# Restore to a stable version
git fetch --tags
git checkout v1.0-stable
sudo systemctl restart trading
```

---

*This is the public reference. Full trading knowledge, lessons, and strategy details are maintained privately.*
