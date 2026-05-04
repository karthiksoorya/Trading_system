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
CAPITAL              = 10_000   # ₹
MAX_RISK_PCT         = 0.01     # 1% of capital per day → ₹100
MAX_TRADES_PER_DAY   = 4
MIN_BOOSTER_SCORE    = 8        # Score < 8 → no trade

# ── Instruments ────────────────────────────────────────────────────────────
NIFTY_SYMBOL = "NSE:NIFTY 50"
VIX_SYMBOL   = "NSE:INDIA VIX"

# ── Session Timings ────────────────────────────────────────────────────────
MARKET_OPEN  = "09:15"
SCAN_START   = "10:05"   # wait till 10 AM before any action
MARKET_CLOSE = "15:30"

# ── Multi-Timeframe Config ─────────────────────────────────────────────────
TF_HIGHER       = "60minute"   # demand/supply curve
TF_INTERMEDIATE = "15minute"   # trend
TF_LOWER        = "5minute"    # entry

# ── Candle Classification Threshold ───────────────────────────────────────
EXCITING_CANDLE_BODY_RATIO = 0.50   # body > 50% of range → exciting

# ── Kite API Credentials (set via environment variables) ──────────────────
# Export in terminal: set KITE_API_KEY=xxx  /  set KITE_API_SECRET=xxx
KITE_API_KEY    = os.getenv("KITE_API_KEY", "")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "")

# ── Token Mode ─────────────────────────────────────────────────────────────
# "manual" → Option A: print URL, paste request_token in terminal (laptop)
# "auto"   → Option B: VPS captures token via HTTP redirect automatically
#   To switch to auto:
#     1. Change to KITE_TOKEN_MODE = "auto"
#     2. Update Kite app redirect URL to http://YOUR_VPS_IP:5000/
#     3. Open port 5000 on VPS firewall
KITE_TOKEN_MODE = "manual"
KITE_TOKEN_PORT = 5000

# ── Computed ───────────────────────────────────────────────────────────────
MAX_DAILY_LOSS   = CAPITAL * MAX_RISK_PCT          # ₹100
RISK_PER_TRADE   = MAX_DAILY_LOSS / MAX_TRADES_PER_DAY  # ₹25

# ── Data dir must exist ────────────────────────────────────────────────────
DATA_DIR.mkdir(exist_ok=True)
CSV_DIR.mkdir(exist_ok=True)
