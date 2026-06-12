# VPS Deployment Commands — Quick Reference

VPS: AWS Lightsail Mumbai | IP: 13.201.210.4 | User: ubuntu

---

## Connect to VPS

Browser SSH (works from anywhere, no port issues):
```
Lightsail Console → trading-system → Connect tab → "Connect using SSH"
```

---

## Daily Routine

```bash
cd ~/Trading_system
source venv/bin/activate
```

Then open browser → `http://13.201.210.4:8501` → Generate Token (Engine tab)

---

## Update Code from GitHub

```bash
cd ~/Trading_system
source venv/bin/activate
git stash          # save any local changes temporarily
git pull
git stash pop      # restore local changes (if any)
```

If `git pull` opens nano editor for merge message:
```
Ctrl + X  → Y → Enter
```

If new dependencies added:
```bash
pip install -r requirements.txt
```

Restart the app:
```bash
sudo systemctl restart trading
```

---

## App Service Management (systemd)

```bash
sudo systemctl start trading      # start
sudo systemctl stop trading       # stop
sudo systemctl restart trading    # restart after code update
sudo systemctl status trading     # check if running
```

View logs:
```bash
journalctl -u trading -f          # live logs, Ctrl+C to exit
```

---

## Manual Run (if systemd not used)

```bash
cd ~/Trading_system
source venv/bin/activate
nohup streamlit run app.py --server.port 8501 --server.address 0.0.0.0 > logs/streamlit.log 2>&1 &
echo $! > streamlit.pid
```

Kill manual process:
```bash
pkill -f streamlit
```

---

## Token Generation (CLI alternative)

```bash
python3 main.py --token
```

---

## Useful Checks

```bash
date                    # verify IST timezone
free -h                 # check memory
df -h                   # check disk space
ps aux | grep streamlit # check if running
ps aux | grep main.py   # check engine process
```

---

## Access Points

- **Streamlit Dashboard:** http://13.201.210.4:8501
- **SSH (browser):** Lightsail Console → Connect using SSH

---

## Common Fixes

| Problem | Fix |
|---|---|
| pip "externally managed" error | Use venv: `source venv/bin/activate` |
| Module not found | `cd ~/Trading_system` first, then run |
| Token invalid | Regenerate daily via Streamlit Engine tab |
| Site not responding | `sudo systemctl restart trading` |
| Memory low | `free -h` → check swap is active |
| Time/market check wrong | `date` → confirm IST, else `sudo timedatectl set-timezone Asia/Kolkata` |
