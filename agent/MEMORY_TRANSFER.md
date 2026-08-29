# Agent Memory Transfer Guide

How to move the agent's learned memory to a new server or machine.

---

## What needs to be transferred

| File | Location on VPS | What it contains |
|------|----------------|-----------------|
| `memory.json` | `~/Trading_system/agent/memory.json` | Trained rules, cautions, win patterns, regime |
| `knowledge/` | `~/Trading_system/agent/knowledge/*.json` | Ingested external sources (Sam Seiden, ICT, etc.) |
| `trades.db` | `~/Trading_system/data/trades.db` | Full trade history (used to re-seed) |

> `memory.json` and `knowledge/` are backed up automatically to Telegram every day at 15:45.

---

## Option A — Download from Telegram (easiest)

Every day at 15:45 the backup job sends two files to your Telegram chat:
- `trades_YYYYMMDD.db`
- `memory_YYYYMMDD.json`

On the new server:
```bash
# Copy the files from your downloads
cp memory_YYYYMMDD.json ~/Trading_system/agent/memory.json
cp trades_YYYYMMDD.db   ~/Trading_system/data/trades.db
```

Then re-ingest your external knowledge sources from the Agent tab in the dashboard.

---

## Option B — SCP from old server

```bash
# Run on your LOCAL machine (not the VPS)
scp ubuntu@YOUR_VPS_IP:~/Trading_system/agent/memory.json     ./memory.json
scp ubuntu@YOUR_VPS_IP:~/Trading_system/agent/knowledge/      ./knowledge/ -r
scp ubuntu@YOUR_VPS_IP:~/Trading_system/data/trades.db        ./trades.db

# Then upload to new server
scp ./memory.json   ubuntu@NEW_SERVER_IP:~/Trading_system/agent/memory.json
scp ./knowledge/    ubuntu@NEW_SERVER_IP:~/Trading_system/agent/knowledge/ -r
scp ./trades.db     ubuntu@NEW_SERVER_IP:~/Trading_system/data/trades.db
```

---

## Option C — Re-seed from scratch on new server

If you only have `trades.db` (no memory.json backup):

```bash
# On new server, after git pull and pip install
python3 agent/seed_memory.py
# Review the candidate file it creates, then promote:
python3 agent/promote_memory.py
```

This creates a candidate memory from all historical trades.
Review it, promote, then re-ingest your external knowledge sources from the Agent tab.

---

## Full migration checklist

```
[ ] git pull on new server (agentic-v2 branch)
[ ] pip install -r requirements.txt
[ ] pip install anthropic pdfplumber
[ ] Copy .env file (KITE, TELEGRAM, ANTHROPIC keys)
[ ] Copy trades.db  → data/trades.db
[ ] Copy memory.json → agent/memory.json    (Option A/B only)
[ ] Copy knowledge/ → agent/knowledge/      (Option A/B only)
[ ] Option C only: python3 agent/seed_memory.py  (creates candidate)
[ ] Option C only: python3 agent/promote_memory.py  (review + promote)
[ ] Set up cron: 09:00 brief.py, 16:00 trainer.py + promote_memory.py
[ ] Restart scheduler.py
[ ] Test: python3 agent/brief.py  (should send Telegram message)
```

---

## What NOT to move

- `.env` — recreate manually on new server (never share this file)
- `gdrive_token.json` / `gdrive_credentials.json` — re-authorise on new server
- Kite token — expires daily, will be refreshed automatically

---

## Verify memory is intact

After transfer, check memory looks correct:
```bash
python3 -c "
import json
m = json.load(open('agent/memory.json'))
print('Last trained :', m.get('last_trained'))
print('Regime       :', m.get('market_regime'))
print('Cautions     :', len(m.get('caution_flags', [])))
print('Win patterns :', len(m.get('win_patterns', [])))
print('Mistakes     :', len(m.get('mistake_log', [])))
"
```
