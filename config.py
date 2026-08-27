import os
from pathlib import Path

# Load .env file if present (pip install python-dotenv)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

# ── Paths ──────────────────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).parent
DATA_DIR  = BASE_DIR / "data"
DB_PATH   = DATA_DIR / "trades.db"
CSV_DIR   = DATA_DIR / "exports"
TOKEN_FILE      = BASE_DIR / ".kite_token"   # ignored by git
ENGINE_PID_FILE = BASE_DIR / ".engine.pid"  # ignored by git

# ── Broker & Mode ──────────────────────────────────────────────────────────
BROKER = "kite"    # "kite" | "upstox"
MODE   = "paper"   # "paper" | "live"

# ── Capital & Risk (S.E.T.S) ───────────────────────────────────────────────
# FIX A: Updated to realistic capital for Nifty options trading.
# One ATM weekly lot costs ~₹3,000–₹8,000 in premium. ₹10,000 was too small
# for meaningful position sizing — risk_per_trade was ₹25 (sub-lot).
# In live mode the system always trades exactly 1 lot; CAPITAL drives the
# daily loss limit and the paper-mode position_size display only.
CAPITAL              = 1_00_000  # ₹1,00,000 (1 lakh) — realistic for 1-lot Nifty options
MAX_RISK_PCT         = 0.01      # 1% of capital per day → ₹1,000
MAX_TRADES_PER_DAY   = 4
MIN_BOOSTER_SCORE    = 8        # Score < 8 → no trade
MIN_CONFLUENCE       = 1        # minimum TFs in agreement to generate signal
NIFTY_LOT_SIZE       = 65       # Nifty 50 lot size — revised by NSE effective Jan 2026 (was 75)

# ── Instruments ────────────────────────────────────────────────────────────
NIFTY_SYMBOL = "NSE:NIFTY 50"
VIX_SYMBOL   = "NSE:INDIA VIX"

# ── VIX Filter ─────────────────────────────────────────────────────────────
# FIX H: Skip signals when India VIX is above this threshold.
# High VIX → inflated option premiums → immediate adverse theta/delta impact.
VIX_MAX      = 20.0   # skip new entries when VIX > 20

# ── IV Rank Filter ─────────────────────────────────────────────────────────
# IV Rank = (current_vix - 52w_low) / (52w_high - 52w_low) × 100
# Skip signals when IV is historically expensive — premium crush risk even on
# correct direction. Aug 25 lesson: PE -₹29 despite +77 pts (IV priced in fear).
# 0 = cheapest IV in 52w. 100 = most expensive. 60 = top-40% = skip.
IV_RANK_MAX  = 60.0   # skip when IV Rank > 60% (set to 100 to disable)

# ── Session Timings ────────────────────────────────────────────────────────
MARKET_OPEN  = "09:15"
SCAN_START   = "10:15"   # FIX G: moved from 10:05 — avoids unreliable opening volatility zones
MARKET_CLOSE = "15:30"

# ── Multi-Timeframe Config ─────────────────────────────────────────────────
TF_HIGHER       = "60minute"   # demand/supply curve
TF_INTERMEDIATE = "15minute"   # trend
TF_LOWER        = "5minute"    # entry

# ── Candle Classification Threshold ───────────────────────────────────────
EXCITING_CANDLE_BODY_RATIO = 0.50   # body > 50% of range → exciting

# ── Stop Loss Buffer ───────────────────────────────────────────────────────
# Extra points beyond the distal line to avoid SL being clipped by wicks.
# Demand: SL = distal - SL_BUFFER_POINTS
# Supply: SL = distal + SL_BUFFER_POINTS
# Set to 0 for pure price action (SL exactly at distal).
SL_BUFFER_POINTS       = 5
SIGNAL_EXPIRY_MINUTES  = 45   # pending signals older than this are auto-expired
ZONE_APPROACH_POINTS   = 50   # LTP must be within this many pts of proximal

# ── Options Exit Rules ─────────────────────────────────────────────────────
# These protect options P&L independently of the index level.
# Theta decay and IV crush can destroy options value even when index is right.
#
# OPTIONS_TRAIL_PCT: if options premium has gained this % from entry cost,
#   lock in profit by exiting. e.g. 30 → exit when CE/PE is up 30%.
#   Set to 0 to disable.
OPTIONS_TRAIL_PCT   = 30    # exit when options up 30% from entry premium
#
# TIME_EXIT_HOUR: close any open trade at this hour (24h) if index target
#   not yet hit. Prevents theta decay from eating gains in the afternoon.
#   e.g. 13 → exit at 13:00 if still open.
#   Set to 0 to disable.
TIME_EXIT_HOUR      = 13    # close at 13:00 if target not reached
#
# DAILY_OPTIONS_TARGET: stop accepting new signals once options P&L for the
#   day reaches this rupee amount. Protects a winning day from giving back
#   gains on follow-on trades.
#   Set to 0 to disable.
DAILY_OPTIONS_TARGET = 0   # ₹0 = disabled

# ── Kite API Credentials (set via environment variables) ──────────────────
# Export in terminal: set KITE_API_KEY=xxx  /  set KITE_API_SECRET=xxx
KITE_API_KEY    = os.getenv("KITE_API_KEY", "")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "")

# ── Upstox API Credentials (set via environment variables) ────────────────
# Export in terminal: set UPSTOX_API_KEY=xxx  /  set UPSTOX_API_SECRET=xxx
# UPSTOX_REDIRECT_URI must match exactly what is set in Upstox developer console.
UPSTOX_API_KEY      = os.getenv("UPSTOX_API_KEY", "")
UPSTOX_API_SECRET   = os.getenv("UPSTOX_API_SECRET", "")
UPSTOX_REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI", f"http://localhost:5000/")

# ── Token Mode (applies to both brokers) ───────────────────────────────────
# "manual" → Option A: print URL, paste auth code in terminal (laptop)
# "auto"   → Option B: VPS captures token via HTTP redirect automatically
#   To switch to auto:
#     1. Change to TOKEN_MODE = "auto"
#     2. Update broker app redirect URL to http://YOUR_VPS_IP:5000/
#     3. Open port 5000 on VPS firewall
KITE_TOKEN_MODE = "auto"
TOKEN_PORT      = 5000

# ── Computed ───────────────────────────────────────────────────────────────
MAX_DAILY_LOSS   = CAPITAL * MAX_RISK_PCT          # ₹100
RISK_PER_TRADE   = MAX_DAILY_LOSS / MAX_TRADES_PER_DAY  # ₹25

# ── Data dir must exist ────────────────────────────────────────────────────
DATA_DIR.mkdir(exist_ok=True)
CSV_DIR.mkdir(exist_ok=True)

# ── User settings (overrides above defaults) ──────────────────────────────
# Written by the Streamlit dashboard; loaded here so the engine picks them up.
SETTINGS_FILE = DATA_DIR / "settings.json"

def load_settings():
    """Return dict of user-saved settings, or {} if file missing."""
    try:
        import json
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}

def save_settings(overrides: dict):
    """Merge overrides into the settings file."""
    import json
    current = load_settings()
    current.update(overrides)
    SETTINGS_FILE.write_text(json.dumps(current, indent=2))

_s = load_settings()
BROKER                = _s.get("BROKER",                BROKER)
MODE                  = _s.get("MODE",                  MODE)
SL_BUFFER_POINTS      = _s.get("SL_BUFFER_POINTS",      SL_BUFFER_POINTS)
SIGNAL_EXPIRY_MINUTES = _s.get("SIGNAL_EXPIRY_MINUTES", SIGNAL_EXPIRY_MINUTES)
MIN_BOOSTER_SCORE     = _s.get("MIN_BOOSTER_SCORE",     MIN_BOOSTER_SCORE)
MIN_CONFLUENCE        = _s.get("MIN_CONFLUENCE",         MIN_CONFLUENCE)
ZONE_APPROACH_POINTS  = _s.get("ZONE_APPROACH_POINTS",  ZONE_APPROACH_POINTS)
SCAN_TIMEFRAMES       = _s.get("SCAN_TIMEFRAMES",       [TF_LOWER, TF_INTERMEDIATE, TF_HIGHER])
SCAN_ZONE_CLASSES     = _s.get("SCAN_ZONE_CLASSES",     ["demand", "supply"])
DAILY_OPTIONS_TARGET  = _s.get("DAILY_OPTIONS_TARGET",  DAILY_OPTIONS_TARGET)
