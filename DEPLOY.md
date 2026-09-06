# VPS Deployment — Quick Reference

**VPS:** AWS Lightsail Mumbai | IP: 13.201.210.4 | User: ubuntu  
**Branch:** agentic-v2

---

## Connect to VPS

```
Lightsail Console → trading-system → Connect tab → "Connect using SSH"
```

Or from terminal:
```bash
ssh -i ~/.ssh/LightsailDefaultKey-ap-south-1.pem ubuntu@13.201.210.4
```

---

## What runs automatically (no daily action needed)

| Time | What | Script | Log |
|------|------|--------|-----|
| 09:00 | Morning brief → Telegram + saves today_context.json | `agent/brief.py` | `logs/brief.log` |
| 09:05 | Engine starts (if not already running) | `main.py --run` | `logs/engine.log` |
| 16:00 | After-market trainer → updates memory.json | `agent/trainer.py` | `logs/trainer.log` |

All three are cron jobs. See `crontab.example` for the exact lines.

**Daily action required:** Login to Kite before 09:15 via dashboard → Engine tab → Generate Token.

---

## Daily Morning Routine

1. Open browser → `http://13.201.210.4:8501`
2. Engine tab → **Generate Token** (Kite login — do this before 09:15)
3. That's it — engine starts automatically at 09:05 via cron

---

## Update Code from GitHub

```bash
cd ~/Trading_system
git pull origin agentic-v2
```

No restart needed for most changes — engine picks up settings.json changes live.  
To restart the engine:
```bash
pkill -f "main.py --run"
# cron restarts it next day at 09:05, or manually:
nohup /home/ubuntu/Trading_system/venv/bin/python main.py --run >> logs/engine.log 2>&1 &
```

---

## Engine Commands

```bash
# Check if running
ps aux | grep "main.py --run"

# Live logs
tail -f ~/Trading_system/logs/engine.log

# Start manually (after Kite token generated)
nohup /home/ubuntu/Trading_system/venv/bin/python main.py --run >> logs/engine.log 2>&1 &

# Stop engine
pkill -f "main.py --run"
```

> **CRITICAL:** Always use `main.py --run` — NEVER `scheduler.py --run`.  
> `scheduler.py` has no `--run` handler and exits immediately after token load.

---

## Crontab

```bash
crontab -l          # view
crontab -e          # edit
```

To install from scratch (fresh VPS or after reset):
```bash
crontab crontab.example
```

---

## What lives on VPS but NOT in git

These must be recreated manually on a fresh VPS:

| File / Config | What it is | How to recreate |
|---|---|---|
| `.env` | API keys (Kite, Telegram, Anthropic) | Create manually — see below |
| `data/settings.json` | Live trading settings | Copy from `settings.template.json`, adjust |
| `data/trades.db` | Trading database | Auto-created on first run |
| `data/today_context.json` | Daily market context | Auto-created by brief.py each morning |
| `agent/memory.json` | Trained trading memory | Run `agent/trainer.py` after enough trades |
| `agent/eval_log.jsonl` | Evaluator audit log | Auto-created by evaluator |
| `venv/` | Python virtual environment | `python3 -m venv venv && venv/bin/pip install -r requirements.txt` |
| `crontab` | Scheduled jobs | `crontab crontab.example` |
| `/etc/resolv.conf` | DNS fix | `echo "nameserver 8.8.8.8" \| sudo tee -a /etc/resolv.conf` |
| `/etc/hosts` | Hostname fix | `echo "127.0.0.1 ip-172-26-4-225" \| sudo tee -a /etc/hosts` |

### `.env` contents (never commit):
```
KITE_API_KEY=your_key
KITE_API_SECRET=your_secret
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
ANTHROPIC_API_KEY=your_anthropic_key
```

---

## Fresh VPS Setup (from scratch)

```bash
# 1. Clone repo
git clone https://github.com/karthiksoorya/Trading_system.git
cd Trading_system
git checkout agentic-v2

# 2. Create venv and install dependencies
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# 3. Create .env (paste your keys)
nano .env

# 4. Copy settings template
cp settings.template.json data/settings.json

# 5. Install crontab
crontab crontab.example

# 6. Fix DNS (if needed)
echo "nameserver 8.8.8.8" | sudo tee -a /etc/resolv.conf

# 7. Set timezone
sudo timedatectl set-timezone Asia/Kolkata

# 8. Start Streamlit dashboard
nohup venv/bin/streamlit run app.py --server.port 8501 --server.address 0.0.0.0 >> logs/streamlit.log 2>&1 &
```

---

## Useful Checks

```bash
date                              # verify IST timezone
free -h                           # memory (warn if < 100Mi available)
ps aux | grep "main.py --run"     # engine running?
ps aux | grep streamlit           # dashboard running?
tail -f logs/engine.log           # live engine output
tail -f logs/brief.log            # this morning's brief
cat data/today_context.json       # what the evaluator knows today
cat agent/memory.json | python3 -m json.tool | head -40   # trained memory
```

---

## Access Points

- **Dashboard:** http://13.201.210.4:8501
- **SSH (browser):** Lightsail Console → Connect using SSH
