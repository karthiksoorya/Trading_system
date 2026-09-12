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

## Daily Pipeline — How Intelligence Flows

```
09:00  agents/brief.py
       READS:  agents/memory.json
       CALLS:  Claude Haiku (brief generation)
       WRITES: data/today_context.json   ← today's regime, cautions, key levels
               data/settings.json       ← regime auto-config (zone classes, scan window)
       SENDS:  Telegram (morning brief message)
          │
          │  data/today_context.json
          │  data/settings.json
          ▼
09:05  main.py --run  (engine starts via cron if not already running)
       READS:  data/settings.json
               data/today_context.json  ← evaluator reads this at each signal
               agents/memory.json        ← evaluator reads this at each signal
       CALLS:  Claude Haiku (TRADE / SKIP / REVIEW per signal)
               Kite API (order execution)
       WRITES: data/trades.db
       SENDS:  Telegram (signal alerts, fills, EOD summary)
          │
          │  data/trades.db
          ▼
16:00  agents/trainer.py
       READS:  data/trades.db
       CALLS:  Claude Haiku (pattern extraction + rule synthesis)
       WRITES: agents/memory.json   ← read by brief.py next morning at 09:00
          │
          ↺  loop — memory.json feeds tomorrow's brief
```

---

## What runs automatically (no daily action needed)

### Daily (Mon–Fri)

| Time | What | Script | Log |
|------|------|--------|-----|
| 09:00 | Morning brief → Telegram + saves today_context.json | `agents/brief.py` | `logs/brief.log` |
| 09:05 | Engine starts (if not already running) | `main.py --run` | `logs/engine.log` |
| 16:00 | After-market trainer → updates memory.json | `agents/trainer.py` | `logs/trainer.log` |
| 16:15 | Learning ingestion → reflects completed trades into knowledge store | `agents/reflect_completed_trades` | `logs/reflection.log` |
| 15:40 | Backup trades.db + agent memory to Telegram — own cron job, NOT scheduled from inside the engine (which exits at 15:35, so it can't fire jobs after that) | `backup.py` | `logs/backup.log` |
| 03:00 | Restart dashboard — clears accumulated memory from long-lived Streamlit process | pkill + relaunch `streamlit run app.py` | `logs/streamlit.log` |

### Weekly (Saturday)

| Time | What | Script | Log |
|------|------|--------|-----|
| 09:00 | Pattern validation → promotes observed patterns to HYPOTHESIS | `agents/validate_patterns` | `logs/validate_patterns.log` |
| 09:15 | Hypothesis validation → chronological train/holdout test | `agents/validate_hypotheses` | `logs/validate_hypotheses.log` |

### Manual (human-triggered)

| When | What | How |
|------|------|-----|
| After weekly validation | Review surviving candidates | `cat logs/validate_hypotheses.log \| python3 -m json.tool` |
| After review | Approve a hypothesis → VALIDATED | `agents/review_knowledge.py` or direct store call with `approved=True` |
| Periodically | Distil validated knowledge into memory.json | Update `agents/memory.json` manually based on validated entries |

All cron jobs are in `crontab.example`. Install with `crontab crontab.example`.

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
| `agents/memory.json` | Trained trading memory | Run `agents/trainer.py` after enough trades |
| `agents/eval_log.jsonl` | Evaluator audit log | Auto-created by evaluator |
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
cat agents/memory.json | python3 -m json.tool | head -40   # trained memory
```

---

## Access Points

- **Dashboard:** http://13.201.210.4:8501
- **SSH (browser):** Lightsail Console → Connect using SSH
