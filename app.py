"""
Nifty Trading System — Streamlit Dashboard
Run: streamlit run app.py

Works on laptop AND VPS:
  Laptop → open http://localhost:8501
  VPS    → open http://YOUR_VPS_IP:8501 from phone browser
"""

import os
import signal
import sqlite3
import subprocess
import sys
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse

import pandas as pd
import streamlit as st

import config
from journal.db import (
    init_db, close_trade, trades_today, daily_pnl,
    get_signals_for_date, get_pending_signals, pending_count,
    approve_signal, reject_signal, reject_all_pending, get_open_trades,
    expire_stale_pending, expire_old_pending,
)
from journal.export import export_day
from scheduler import is_market_open, get_last_trading_day


def _get_ltp() -> float | None:
    """Fetch live Nifty LTP. Returns None if broker not connected."""
    try:
        from brokers.kite_adapter import KiteAdapter
        return KiteAdapter().get_ltp(config.NIFTY_SYMBOL)
    except Exception:
        return None

# ── Page setup ────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Nifty Trading System",
    page_icon="📈",
    layout="wide",
)

# ── Login gate — Telegram OTP ─────────────────────────────────────────────
import secrets
from datetime import timedelta as _td_login

_TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
_TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def _send_login_otp() -> str:
    otp = f"{secrets.randbelow(1_000_000):06d}"
    if _TG_TOKEN and _TG_CHAT_ID:
        import requests as _req
        _req.post(
            f"https://api.telegram.org/bot{_TG_TOKEN}/sendMessage",
            json={
                "chat_id": _TG_CHAT_ID,
                "text": f"🔐 <b>Login OTP</b>: <code>{otp}</code>\nExpires in 5 minutes.",
                "parse_mode": "HTML",
            },
            timeout=5,
        )
    return otp


_current_mode = config.load_settings().get("MODE", "paper")
_is_live_mode = False   # OTP gate temporarily disabled

if _is_live_mode and not st.session_state.get("authenticated"):
    st.markdown(
        """
        <style>
        [data-testid="stAppViewContainer"] > .main { display:flex; justify-content:center; }
        .login-wrap { width:340px; margin-top:8rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown('<div class="login-wrap">', unsafe_allow_html=True)
    st.markdown("## 🔐 Nifty Trading System")
    st.error("🔴 Live mode — OTP required.")

    if not _TG_TOKEN or not _TG_CHAT_ID:
        st.error("Telegram not configured in .env — cannot send OTP.")
    elif not st.session_state.get("_otp_sent"):
        st.caption("A one-time code will be sent to your Telegram.")
        if st.button("📲 Send OTP to Telegram", type="primary", use_container_width=True):
            _otp = _send_login_otp()
            st.session_state["_otp"]         = _otp
            st.session_state["_otp_expiry"]  = datetime.now() + _td_login(minutes=5)
            st.session_state["_otp_sent"]    = True
            st.rerun()
    else:
        _remaining = st.session_state["_otp_expiry"] - datetime.now()
        if _remaining.total_seconds() <= 0:
            st.warning("OTP expired.")
            if st.button("Resend OTP", use_container_width=True):
                st.session_state.pop("_otp_sent", None)
                st.rerun()
        else:
            mins = int(_remaining.total_seconds() // 60)
            secs = int(_remaining.total_seconds() % 60)
            st.caption(f"Check Telegram. Code expires in {mins}m {secs}s.")
            with st.form("_otp_form", clear_on_submit=False):
                _entered = st.text_input("Enter 6-digit OTP", max_chars=6,
                                         placeholder="000000", key="_otp_input")
                c1, c2 = st.columns(2)
                _verify  = c1.form_submit_button("Verify", type="primary", use_container_width=True)
                _resend  = c2.form_submit_button("Resend", use_container_width=True)

            if _verify:
                if _entered == st.session_state.get("_otp"):
                    for k in ("_otp", "_otp_expiry", "_otp_sent"):
                        st.session_state.pop(k, None)
                    st.session_state["authenticated"] = True
                    st.rerun()
                else:
                    st.error("Wrong OTP — try again.")
            if _resend:
                _otp = _send_login_otp()
                st.session_state["_otp"]        = _otp
                st.session_state["_otp_expiry"] = datetime.now() + _td_login(minutes=5)
                st.rerun()

    st.divider()
    st.caption("Not trading live right now?")
    if st.button("↩ Switch back to Paper Mode", use_container_width=True):
        config.save_settings({"MODE": "paper"})
        for k in ("_otp", "_otp_expiry", "_otp_sent", "authenticated"):
            st.session_state.pop(k, None)
        st.rerun()

    st.markdown('</div>', unsafe_allow_html=True)
    st.stop()

init_db()
expire_stale_pending()
expire_old_pending(config.load_settings().get("SIGNAL_EXPIRY_MINUTES", config.SIGNAL_EXPIRY_MINUTES))

# ── Engine helpers ────────────────────────────────────────────────────────

def _engine_pid() -> int | None:
    try:
        return int(config.ENGINE_PID_FILE.read_text().strip())
    except Exception:
        return None

def is_engine_running() -> bool:
    # Primary check: flag in settings.json (works even under systemd)
    if config.load_settings().get("engine_state") == "stopped":
        config.ENGINE_PID_FILE.unlink(missing_ok=True)
        return False
    # Secondary check: is the PID actually alive?
    pid = _engine_pid()
    if pid is None:
        return False
    try:
        if sys.platform == "win32":
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                stderr=subprocess.DEVNULL,
            ).decode(errors="ignore")
            alive = str(pid) in out
        else:
            os.kill(pid, 0)
            alive = True
    except Exception:
        alive = False
    if not alive:
        config.ENGINE_PID_FILE.unlink(missing_ok=True)
    return alive

def start_engine():
    config.save_settings({"engine_state": "running"})
    proc = subprocess.Popen(
        [sys.executable, str(config.BASE_DIR / "main.py"), "--run"],
        cwd=str(config.BASE_DIR),
    )
    config.ENGINE_PID_FILE.write_text(str(proc.pid))

def stop_engine():
    # Set flag first — engine loop will exit gracefully within 30s
    config.save_settings({"engine_state": "stopped"})
    pid = _engine_pid()
    if pid:
        try:
            if sys.platform == "win32":
                subprocess.call(
                    ["taskkill", "/F", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            else:
                os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    try:
        config.ENGINE_PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass

def _extract_token(raw: str) -> str:
    if raw.startswith("http"):
        params = parse_qs(urlparse(raw).query)
        return params.get("request_token", [""])[0]
    return raw.strip()

# ── Sidebar ───────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("📈 Nifty System")
    st.divider()

    engine_running = st.session_state.get("engine_on", is_engine_running())
    st.metric("Engine",       "🟢 Running" if engine_running else "🔴 Stopped")
    _pending = pending_count()
    st.metric("Pending",      f"🔔 {_pending} signal(s)" if _pending else "✅ None")
    st.metric("Trades Today", trades_today())
    st.metric("Daily P&L",    f"{daily_pnl():.2f} pts")
    st.divider()

    # ── Trading Mode switcher ─────────────────────────────────────────────
    _sb_mode = config.load_settings().get("MODE", "paper")
    if _sb_mode == "live":
        st.error("🔴 LIVE MODE")
        try:
            from brokers.kite_adapter import KiteAdapter as _KA
            _funds = _KA().get_funds()
            if _funds["ok"]:
                st.metric("Available Margin", f"₹{_funds['live_balance']:,.0f}")
                st.metric("Used Margin",      f"₹{_funds['used']:,.0f}")
                if _funds["live_balance"] < config.CAPITAL:
                    st.warning(f"⚠️ Margin ₹{_funds['live_balance']:,.0f} is below capital ₹{config.CAPITAL:,}")
            else:
                st.caption(f"Margin fetch failed: {_funds.get('error','')}")
        except Exception:
            pass
    else:
        st.success("🟢 PAPER MODE")

    if _sb_mode == "paper":
        if st.button("Switch to 🔴 LIVE", use_container_width=True):
            st.session_state["_sb_switch_live"] = True
        if st.session_state.get("_sb_switch_live"):
            _confirm = st.text_input("Type LIVE to confirm", placeholder="LIVE", key="_sb_live_confirm")
            if st.button("Confirm LIVE", type="primary", use_container_width=True):
                if _confirm == "LIVE":
                    config.save_settings({"MODE": "live"})
                    st.session_state.pop("_sb_switch_live", None)
                    st.success("Switched. Restart engine.")
                    st.rerun()
                else:
                    st.error("Type LIVE exactly.")
    else:
        if st.button("Switch to 🟢 PAPER", use_container_width=True):
            config.save_settings({"MODE": "paper"})
            st.session_state["authenticated"] = False
            st.success("Switched to Paper. Restart engine.")
            st.rerun()

    st.divider()

    if st.button("🔄 Refresh page"):
        st.rerun()

    st.caption(f"Updated: {datetime.now().strftime('%H:%M:%S')}")
    if _is_live_mode:
        st.divider()
        if st.button("🚪 Logout", use_container_width=True):
            st.session_state.clear()
            st.rerun()

# ── Engine panel ─────────────────────────────────────────────────────────

def _engine_panel():
    import threading

    # Initialise session state from real process check on first load only.
    # After that, button clicks update session_state directly so the UI
    # reflects the intended state immediately — no timing race with st.rerun().
    if "engine_on" not in st.session_state:
        st.session_state.engine_on = is_engine_running()

    running = st.session_state.engine_on

    # Status badge
    if running:
        st.success(f"🟢 Engine is RUNNING  (PID {_engine_pid()})")
    else:
        st.warning("🔴 Engine is STOPPED")

    c1, c2, c3 = st.columns(3)

    # ── Start ─────────────────────────────────────────────────────────────
    if c1.button(
        "▶ Start Engine",
        disabled=running,
        type="primary" if not running else "secondary",
        use_container_width=True,
        help="Starts the scanner. Waits until 10:05 AM then scans every 5 min.",
    ):
        try:
            start_engine()
            st.session_state.engine_on = True
            st.rerun()
        except Exception as e:
            st.error(f"Failed to start: {e}")

    # ── Stop ──────────────────────────────────────────────────────────────
    if c2.button(
        "⏹ Stop Engine",
        disabled=not running,
        type="primary" if running else "secondary",
        use_container_width=True,
        help="Stops the background engine process.",
    ):
        stop_engine()
        st.session_state.engine_on = False
        st.rerun()

    # ── Scan Now ──────────────────────────────────────────────────────────
    if c3.button(
        "⚡ Scan Now",
        type="primary",
        use_container_width=True,
        help="One-time scan — bypasses market hours. Good for testing.",
    ):
        from scheduler import scan_now
        def _run():
            scan_now()
        threading.Thread(target=_run, daemon=True).start()
        if is_market_open():
            st.toast("Scan started — check Signals tab in ~10 seconds.", icon="⚡")
        else:
            st.toast(
                f"Market is closed. Scanning last trading day ({get_last_trading_day()}) "
                "data for testing — results may not reflect live conditions.",
                icon="⚠️",
            )


# ── Tabs ──────────────────────────────────────────────────────────────────
_pending_label = f"🔔 Approvals ({pending_count()})" if pending_count() else "🔔 Approvals"
tab_approvals, tab_engine, tab_signals, tab_performance, tab_learning, tab_tutorial, tab_zones, tab_agent = st.tabs([
    _pending_label, "🔧 Engine", "📊 Signals", "📈 Performance", "🤖 Learning", "📖 Tutorial", "🔍 Zones", "🧠 Agent"
])


# ══════════════════════════════════════════════════════════════════════════
# TAB 1 — ENGINE CONTROL
# ══════════════════════════════════════════════════════════════════════════
with tab_engine:
    st.header("Engine Control")

    # ── Token ─────────────────────────────────────────────────────────────
    st.subheader("1. Generate Today's Token")
    st.caption("Required once every morning. Kite token expires at midnight.")

    try:
        from brokers.kite_adapter import KiteAdapter
        k = KiteAdapter()
        login_url = k.generate_login_url()
    except Exception as e:
        st.error(f"Could not generate login URL: {e}")
        login_url = None
        k = None

    # Token status
    _token_today = False
    if config.TOKEN_FILE.exists():
        try:
            import json as _json
            _td = _json.loads(config.TOKEN_FILE.read_text())
            _token_today = _td.get("date") == date.today().isoformat()
        except Exception:
            pass

    if _token_today:
        st.success("✅ Token valid for today — engine is ready to start.")
    else:
        st.warning("⚠️ No valid token yet. Complete the steps below.")

    st.divider()

    # Step 1: Open login URL
    st.markdown("**Step 1 — Open Kite login link**")
    if login_url:
        st.markdown(f"### [🔑 Click here to Login to Kite]({login_url})")
    st.caption("Tap the link above. Login with your Kite password + TOTP. Your browser will show 'Site can't be reached' — that's normal.")

    st.divider()

    # Step 2: Paste token
    st.markdown("**Step 2 — Paste the token**")

    # Auto-capture: if Kite redirected back to THIS app, request_token is in the URL params
    _auto_token = ""
    try:
        _qp = st.query_params
        if "request_token" in _qp:
            _auto_token = _qp["request_token"]
            st.query_params.clear()   # remove from address bar
            st.info(f"✅ Token auto-captured from redirect URL.")
    except Exception:
        pass

    if not _auto_token:
        st.caption("Copy the full URL from the address bar and paste below (or just the token value after `request_token=`).")

    # Pre-flight: warn if API secret is missing
    if not config.KITE_API_SECRET:
        st.error("⚠️ KITE_API_SECRET is not set in your .env file. Token exchange will fail until this is fixed.")

    with st.form("_token_form"):
        raw_url = st.text_input(
            "Paste redirect URL or just the request_token value",
            value=_auto_token,
            placeholder="http://127.0.0.1/?request_token=XXXXXX  OR  just XXXXXX",
        )
        if st.form_submit_button("💾 Save Token", type="primary", use_container_width=True):
            token = _extract_token(raw_url)
            if not token:
                st.error("Could not read token. Paste the full URL or the token value.")
            elif not config.KITE_API_SECRET:
                st.error("KITE_API_SECRET is missing in .env — cannot complete token exchange.")
            else:
                try:
                    with st.spinner("Exchanging token with Kite..."):
                        k.generate_session(token)
                    with st.spinner("Pre-loading instruments (makes first trade instant)..."):
                        try:
                            k.prefetch_instruments()
                        except Exception:
                            pass
                    st.success("✅ Token saved! Engine is ready to start.")
                    st.balloons()
                except Exception as e:
                    import traceback
                    st.error(f"**{type(e).__name__}**: {e}")
                    st.code(traceback.format_exc())

    st.divider()

    # ── Engine start / stop ───────────────────────────────────────────────
    st.subheader("2. Engine")
    _engine_panel()  # isolated fragment — only this section reruns on button click

    st.divider()

    # ── Status ────────────────────────────────────────────────────────────
    st.subheader("3. Today's Status")
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Trades Today",  trades_today())
    s2.metric("Max Per Day",   config.MAX_TRADES_PER_DAY)
    s3.metric("Daily P&L",     f"{daily_pnl():.2f} pts")
    s4.metric("Max Daily Loss", f"₹{config.MAX_DAILY_LOSS:.0f}")

    token_ok = config.TOKEN_FILE.exists()
    token_date = ""
    if token_ok:
        import json
        try:
            data = json.loads(config.TOKEN_FILE.read_text())
            token_date = data.get("date", "")
            token_ok = token_date == date.today().isoformat()
        except Exception:
            token_ok = False

    st.markdown(
        f"Token: {'✅ Valid for today' if token_ok else '❌ Missing or expired — generate token first'}"
    )
    st.markdown(
        f"Engine: {'🟢 Running (PID ' + str(_engine_pid()) + ')' if st.session_state.get('engine_on', False) else '🔴 Not running'}"
    )

    st.divider()

    # ── Live Connection Check ─────────────────────────────────────────────
    st.subheader("4. Live Connection Check")
    st.caption("Verify Kite API is reachable and data is correct before trading.")
    if st.button("🔍 Run Check", use_container_width=True, disabled=not token_ok):
        from brokers.kite_adapter import KiteAdapter as _KAC
        _kc = _KAC()
        with st.spinner("Fetching from Kite..."):
            # Funds
            try:
                _funds = _kc.get_funds()
                _funds_ok = True
            except Exception as _fe:
                _funds = {}
                _funds_ok = False
                _funds_err = str(_fe)

            # Nifty LTP
            try:
                _ltp = _kc.get_ltp(config.NIFTY_SYMBOL)
                _ltp_ok = True
            except Exception as _le:
                _ltp = 0.0
                _ltp_ok = False
                _ltp_err = str(_le)

            # Next contract (may not be available outside market hours)
            try:
                _contract = _kc.get_options_contract(_ltp or 0, "demand")
                _contract_ok = True
            except Exception as _ce:
                _contract = {}
                _contract_ok = False
                _contract_err = str(_ce)

        f1, f2 = st.columns(2)
        if _funds_ok:
            f1.metric("Available Cash", f"₹{_funds.get('cash', 0):,.0f}")
        else:
            f1.error(f"Funds: {_funds_err}")

        if _ltp_ok:
            f2.metric("Nifty LTP", f"{_ltp:,.1f}")
        else:
            f2.error(f"LTP: {_ltp_err}")

        if _contract_ok:
            st.success(
                f"**Next Contract:** {_contract['symbol']} | "
                f"Strike {_contract['strike']} {_contract['option_type']} | "
                f"Expiry {_contract['expiry']} | Lot size {_contract['lot_size']}"
            )
        else:
            st.warning(f"Contract lookup: {_contract_err} — normal outside market hours.")

    st.divider()

    # ── Settings ──────────────────────────────────────────────────────────
    st.subheader("5. Settings")
    st.caption(
        "This screen is the source of truth — it writes data/settings.json, which "
        "overrides config.py. Most changes apply on the next scan; broker change needs "
        "an engine restart."
    )

    _current = config.load_settings()

    with st.expander("📋 Active config right now (settings.json + code defaults merged)"):
        _eff = {
            "SCAN_ZONE_CLASSES":    _current.get("SCAN_ZONE_CLASSES", config.SCAN_ZONE_CLASSES),
            "MIN_BOOSTER_SCORE":    _current.get("MIN_BOOSTER_SCORE", config.MIN_BOOSTER_SCORE),
            "MIN_CONFLUENCE":       _current.get("MIN_CONFLUENCE", config.MIN_CONFLUENCE),
            "MIN_RISK_POINTS":      _current.get("MIN_RISK_POINTS", config.MIN_RISK_POINTS),
            "ZONE_APPROACH_POINTS": _current.get("ZONE_APPROACH_POINTS", config.ZONE_APPROACH_POINTS),
            "MAX_TRADES_PER_DAY":   _current.get("MAX_TRADES_PER_DAY", config.MAX_TRADES_PER_DAY),
            "AUTO_FIRST_TRADE":     _current.get("AUTO_FIRST_TRADE", False),
            "AUTO_FIRST_COUNT":     _current.get("AUTO_FIRST_COUNT", config.AUTO_FIRST_COUNT),
            "SCAN_INTERVAL_MINUTES": _current.get("SCAN_INTERVAL_MINUTES", config.SCAN_INTERVAL_MINUTES),
            "VIX_MAX":              _current.get("VIX_MAX", config.VIX_MAX),
            "TIME_EXIT_HOUR":       _current.get("TIME_EXIT_HOUR", config.TIME_EXIT_HOUR),
            "IV_RANK_MAX":          _current.get("IV_RANK_MAX", config.IV_RANK_MAX),
            "OPTIONS_TRAIL_PCT":    _current.get("OPTIONS_TRAIL_PCT", config.OPTIONS_TRAIL_PCT),
            "OPTIONS_SL_PCT":       _current.get("OPTIONS_SL_PCT", config.OPTIONS_SL_PCT),
            "SCAN_WINDOW":          _current.get("SCAN_WINDOW", {"start": "09:15", "end": "10:30"}),
            "MODE":                 _current.get("MODE", config.MODE),
        }
        st.json(_eff)
        _from_file = [k for k in _eff if k in _current]
        st.caption(f"From settings.json: {', '.join(_from_file) or 'none'} · rest are code defaults")

    # ── Broker ────────────────────────────────────────────────────────────
    _broker_options = ["kite", "upstox"]
    _broker_idx = _broker_options.index(_current.get("BROKER", config.BROKER)) \
                  if _current.get("BROKER", config.BROKER) in _broker_options else 0
    broker_choice = st.selectbox(
        "Broker",
        options=_broker_options,
        index=_broker_idx,
        help="Switch between Kite and Upstox. Restart engine after changing.",
        format_func=lambda b: {"kite": "Zerodha Kite", "upstox": "Upstox"}.get(b, b),
    )
    if broker_choice != _current.get("BROKER", config.BROKER):
        st.warning("Broker changed — save and restart engine to apply.", icon="⚠️")

    st.divider()

    sl_buffer = st.number_input(
        "Stop Loss Buffer (points beyond distal line)",
        min_value=0, max_value=30,
        value=int(_current.get("SL_BUFFER_POINTS", config.SL_BUFFER_POINTS)),
        step=1,
        help="0 = SL exactly at zone edge. 5–10 = buffer to avoid wick stop-outs.",
    )

    all_tfs = [config.TF_LOWER, config.TF_INTERMEDIATE, config.TF_HIGHER]

    entry_tf = st.selectbox(
        "Entry Timeframe (generates signals)",
        options=all_tfs,
        index=all_tfs.index(_current.get("ENTRY_TIMEFRAME", config.TF_LOWER)),
        help="Only this TF generates entry signals. The other two are used for confluence scoring only.",
    )

    scan_tfs = st.multiselect(
        "Confluence Timeframes (include in scan)",
        options=all_tfs,
        default=_current.get("SCAN_TIMEFRAMES", all_tfs),
        help="TFs used to build confluence. Entry TF is always included automatically.",
    )

    scan_classes = st.multiselect(
        "Zone Classes",
        options=["demand", "supply"],
        default=_current.get("SCAN_ZONE_CLASSES", config.SCAN_ZONE_CLASSES),
        help="3-year backtest: demand/CE had negative expectancy. Default is supply-only (PE).",
    )

    expiry_minutes = st.slider(
        "Signal Expiry Window (minutes)",
        min_value=15, max_value=120,
        value=_current.get("SIGNAL_EXPIRY_MINUTES", config.SIGNAL_EXPIRY_MINUTES),
        step=5,
        help="Pending signals older than this are auto-expired. Default 45 min.",
    )

    zone_approach = st.number_input(
        "Zone Approach Distance (points)",
        min_value=10, max_value=200,
        value=int(_current.get("ZONE_APPROACH_POINTS", config.ZONE_APPROACH_POINTS)),
        step=5,
        help="Signal only fires if LTP is within this many points of the zone proximal. "
             "30 = tight (enter near the line). Raising it enters further into the move — worse R:R.",
    )

    min_risk = st.number_input(
        "Min Risk (points)",
        min_value=0, max_value=60,
        value=int(_current.get("MIN_RISK_POINTS", config.MIN_RISK_POINTS)),
        step=1,
        help="Skip signals whose entry-to-SL distance is below this. Degenerate tiny zones "
             "give a 2R target only a few points away. 0 = off.",
    )

    col_score, col_conf = st.columns(2)
    min_score = col_score.slider(
        "Min Booster Score",
        min_value=8, max_value=10,
        value=_current.get("MIN_BOOSTER_SCORE", config.MIN_BOOSTER_SCORE),
        step=1,
        help="8 = standard, 9 = good setups only, 10 = perfect setups only.",
    )
    min_conf = col_conf.slider(
        "Min Confluence (TFs)",
        min_value=1, max_value=3,
        value=_current.get("MIN_CONFLUENCE", config.MIN_CONFLUENCE),
        step=1,
        help="1 = any signal, 2 = confirmed by 2 TFs, 3 = all 3 TFs agree.",
    )

    col_maxtr, col_vix = st.columns(2)
    max_trades = col_maxtr.number_input(
        "Max Trades Per Day",
        min_value=1, max_value=10, step=1,
        value=int(_current.get("MAX_TRADES_PER_DAY", config.MAX_TRADES_PER_DAY)),
        help="Hard daily cap. Once this many trades are taken, no more signals today.",
    )
    vix_max = col_vix.number_input(
        "Max India VIX",
        min_value=10.0, max_value=40.0, step=0.5,
        value=float(_current.get("VIX_MAX", config.VIX_MAX)),
        help="Skip all scans when India VIX is above this. High VIX = inflated premiums.",
    )

    scan_interval = st.number_input(
        "Scan interval (minutes) — restart engine to apply",
        min_value=1, max_value=5, step=1,
        value=int(_current.get("SCAN_INTERVAL_MINUTES", config.SCAN_INTERVAL_MINUTES)),
        help="How often the engine re-checks for signals. 1 = react within ~1 min of "
             "price reaching a zone (cuts signal-to-entry lag). 5 = old behaviour.",
    )

    st.markdown("**Scan Window** — restrict scanning to specific market hours (IST)")
    _scan_win = _current.get("SCAN_WINDOW", {"start": "09:15", "end": "10:30"})
    _sw_cols = st.columns(2)
    scan_start_time = _sw_cols[0].text_input(
        "Scan from (HH:MM)", value=_scan_win["start"],
        help="e.g. 10:00 — skip the opening volatility window",
    )
    scan_end_time = _sw_cols[1].text_input(
        "Scan until (HH:MM)", value=_scan_win["end"],
        help="e.g. 15:00 — stop before end-of-day rush",
    )
    st.caption("Default 09:15 → 10:30 — the golden window from live trade analysis. Widen only if you want afternoon signals (data shows 10:xx+ options trades are consistently losing).")

    st.divider()
    st.subheader("Options Exit Rules")
    st.caption(
        "Both rules watch the live options premium every scan cycle — "
        "independent of where the Nifty index is."
    )
    _opt_cols = st.columns(2)
    options_trail_pct = _opt_cols[0].number_input(
        "Profit lock — exit when options up (%)",
        min_value=0, max_value=100, step=5,
        value=int(_current.get("OPTIONS_TRAIL_PCT", config.OPTIONS_TRAIL_PCT)),
        help="Exit when options premium has gained this % from entry. 0 = disabled. Default 30.",
    )
    options_sl_pct = _opt_cols[1].number_input(
        "Loss cut — exit when options down (%)",
        min_value=0, max_value=100, step=5,
        value=int(_current.get("OPTIONS_SL_PCT", config.OPTIONS_SL_PCT)),
        help="Exit when options premium has fallen this % from entry. 0 = disabled. Default 20.",
    )
    time_exit_hour = st.number_input(
        "Stop new signals & close open trades at hour (0 = disabled)",
        min_value=0, max_value=15, step=1,
        value=int(_current.get("TIME_EXIT_HOUR", config.TIME_EXIT_HOUR)),
        help="e.g. 13 = close any open trade at 13:00 and stop new signals after that. Prevents afternoon theta decay.",
    )

    st.divider()
    st.subheader("IV Rank Filter")
    st.caption(
        "IV Rank = where today's VIX sits within its 52-week range (0% = cheapest, 100% = most expensive). "
        "High IV Rank means option premiums are historically rich — IV crush risk even when direction is right."
    )
    iv_rank_max = st.slider(
        "Max IV Rank to allow signal (%)",
        min_value=30, max_value=100,
        value=int(_current.get("IV_RANK_MAX", 60)),
        step=5,
        help=(
            "Signal is blocked when IV Rank exceeds this. "
            "60 = skip when premium is in top 40% of 52-week range. "
            "Set to 100 to disable the filter entirely."
        ),
    )
    _iv_rank_cols = st.columns(3)
    _iv_rank_cols[0].metric("🟢 Cheap IV", "≤ 30%", "Buy freely")
    _iv_rank_cols[1].metric("🟡 Moderate IV", "31–60%", "Caution")
    _iv_rank_cols[2].metric("🔴 Rich IV", "> 60%", "IV crush risk")

    st.divider()
    st.subheader("Auto-Trade First Signals")
    _af_cols = st.columns([2, 1])
    auto_first = _af_cols[0].toggle(
        "🤖 Auto-execute the first trades of the day",
        value=_current.get("AUTO_FIRST_TRADE", False),
        help=(
            "When ON: the first N qualifying signals each day are approved and ordered "
            "automatically — no Telegram button press needed. This also removes the "
            "approval-latency loss (backtest: tapping Approve 2-5 min late destroys the edge). "
            "Signals beyond N still require your manual approval."
        ),
    )
    auto_first_count = _af_cols[1].number_input(
        "How many",
        min_value=1, max_value=10, step=1,
        value=int(_current.get("AUTO_FIRST_COUNT", config.AUTO_FIRST_COUNT)),
        disabled=not auto_first,
    )
    if auto_first:
        st.info(
            f"🤖 **Auto-trade ON** — the first **{auto_first_count}** signal(s) today are placed "
            "automatically on Kite. Telegram notifies you when each executes. "
            "Signals after that still need your approval.",
            icon="🤖",
        )
    else:
        st.caption("Auto-trade OFF — every signal requires your approval via Telegram.")

    st.divider()
    st.subheader("Fully Automated Mode")
    fully_auto = st.toggle(
        "🚀 Auto-execute ALL signals (no manual approval)",
        value=_current.get("FULLY_AUTOMATED", False),
        help=(
            "When ON: every qualifying signal is placed on Kite automatically — no Telegram approval needed. "
            "All existing filters still apply (score, VIX, time-of-day, daily loss limit, max trades). "
            "You will still receive Telegram notifications with an early exit button. "
            "Use only when you have tested the system sufficiently in paper/semi-auto mode."
        ),
    )
    if fully_auto:
        st.warning(
            "🚀 **Fully automated ON** — system will place ALL qualifying orders without asking you. "
            "Ensure VIX filter, score threshold, and daily loss limit are set correctly before leaving this on.",
            icon="⚠️",
        )
    else:
        st.caption("Fully automated OFF — signals above require manual or first-trade auto only.")

    st.divider()
    st.subheader("Daily Options P&L Target")
    daily_opts_target = st.number_input(
        "Stop new trades when options profit reaches (₹)",
        min_value=0,
        max_value=50000,
        step=100,
        value=int(_current.get("DAILY_OPTIONS_TARGET", config.DAILY_OPTIONS_TARGET)),
        help=(
            "Once your options P&L for the day hits this amount, no new signals will be accepted. "
            "Set to 0 to disable. Example: 500 → stop after ₹500 options profit. "
            "Protects a winning day from giving back gains on follow-on trades."
        ),
    )
    if daily_opts_target > 0:
        st.info(
            f"🎯 New trades will stop once options P&L reaches **₹{daily_opts_target:,}** today.",
            icon="🎯",
        )
    else:
        st.caption("Daily options target OFF — no profit-based trade limit.")

    if st.button("💾 Save Settings"):
        if not scan_tfs:
            st.error("Select at least one timeframe.")
        elif not scan_classes:
            st.error("Select at least one zone class.")
        else:
            config.save_settings({
                "BROKER":                broker_choice,
                "SL_BUFFER_POINTS":      sl_buffer,
                "ENTRY_TIMEFRAME":       entry_tf,
                "SCAN_TIMEFRAMES":       scan_tfs,
                "SCAN_ZONE_CLASSES":     scan_classes,
                "SIGNAL_EXPIRY_MINUTES": expiry_minutes,
                "ZONE_APPROACH_POINTS":  zone_approach,
                "MIN_RISK_POINTS":       min_risk,
                "MIN_BOOSTER_SCORE":     min_score,
                "MIN_CONFLUENCE":        min_conf,
                "MAX_TRADES_PER_DAY":    max_trades,
                "VIX_MAX":               vix_max,
                "SCAN_INTERVAL_MINUTES": scan_interval,
                "SCAN_WINDOW":           {"start": scan_start_time, "end": scan_end_time},
                "IV_RANK_MAX":           iv_rank_max,
                "AUTO_FIRST_TRADE":      auto_first,
                "AUTO_FIRST_COUNT":      auto_first_count,
                "FULLY_AUTOMATED":       fully_auto,
                "DAILY_OPTIONS_TARGET":  daily_opts_target,
                "OPTIONS_TRAIL_PCT":     options_trail_pct,
                "OPTIONS_SL_PCT":        options_sl_pct,
                "TIME_EXIT_HOUR":        time_exit_hour,
            })
            st.success(
                f"Saved — TF: {entry_tf} | Score ≥ {min_score} | "
                f"Confluence ≥ {min_conf} TF | Approach ≤ {zone_approach} pts | "
                f"Expiry: {expiry_minutes} min | Window: {scan_start_time}–{scan_end_time}"
            )

    st.divider()

    # ── Backup ────────────────────────────────────────────────────────────
    st.subheader("6. Backup")
    col_db, col_csv = st.columns(2)

    with col_db:
        if config.DB_PATH.exists():
            with open(config.DB_PATH, "rb") as f:
                col_db.download_button(
                    label="⬇️ Download trades.db",
                    data=f,
                    file_name=f"trades_{date.today().isoformat()}.db",
                    mime="application/octet-stream",
                    use_container_width=True,
                    help="Download the full trade database. Save to Google Drive or anywhere.",
                )

    with col_csv:
        today_csv = config.CSV_DIR / f"trades_{date.today().isoformat()}.csv"  # BUG 6 fix: match export.py naming
        if today_csv.exists():
            with open(today_csv, "rb") as f:
                col_csv.download_button(
                    label="⬇️ Download Today's CSV",
                    data=f,
                    file_name=today_csv.name,
                    mime="text/csv",
                    use_container_width=True,
                )
        else:
            col_csv.button("⬇️ Download Today's CSV", disabled=True,
                           use_container_width=True, help="No CSV yet for today.")

    st.markdown("**📨 Telegram Backup**")
    if st.button("📨 Send trades.db to Telegram", use_container_width=True,
                 help="Sends trades.db as a file to your Telegram chat. Download from phone → upload to Drive."):
        with st.spinner("Sending to Telegram..."):
            try:
                import backup as _backup
                ok = _backup.run_backup()
                if ok:
                    st.success("✅ File sent to Telegram. Download from your chat.")
                else:
                    st.error("❌ Send failed — check logs/backup.log for details.")
            except Exception as e:
                st.error(f"❌ Error: {e}")
    st.caption("Auto-backup also runs daily at 15:45 IST after market close.")

# ══════════════════════════════════════════════════════════════════════════
# TAB 2 — APPROVALS  (auto-refreshes every 30s without changing active tab)
# ══════════════════════════════════════════════════════════════════════════

@st.fragment(run_every=30)
def _approvals_tab():
    # ── Expiry day banner ─────────────────────────────────────────────────
    from datetime import date as _date, timedelta as _td
    _today = _date.today()
    if _today.weekday() == 1:   # Tuesday = expiry day
        _next_exp = _today + _td(days=7)
        st.warning(
            f"⚠️ **Today is Nifty expiry day (Tuesday).** "
            f"Orders will use next week's contract ({_next_exp.strftime('%d %b %Y')}) — not today's expiry.",
            icon="⚠️",
        )

    # ── Open Trades — Live P&L ────────────────────────────────────────────
    open_trades = get_open_trades()
    if open_trades:
        st.subheader("📊 Open Trades — Live P&L")
        ltp = _get_ltp()

        for row in open_trades:
            t = dict(row)
            entry      = t["entry"]
            sl         = t["stop_loss"]
            target     = t["intraday_target"]
            zone_class = t["zone_class"]

            if ltp is not None:
                unreal    = (ltp - entry) if zone_class == "demand" else (entry - ltp)
                to_target = abs(target - ltp)
                to_sl     = abs(ltp - sl)
            else:
                unreal = to_target = to_sl = None

            zone_label = f"🟢 DEMAND {t['zone_type']}" if zone_class == "demand" else f"🔴 SUPPLY {t['zone_type']}"
            with st.container(border=True):
                st.markdown(f"**#{t['id']} — {zone_label} | {t['timeframe']}**")
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.metric("Entry",  f"{entry:.2f}")
                c2.metric("LTP",    f"{ltp:.2f}" if ltp else "—")
                c3.metric(
                    "Unrealized P&L",
                    f"{unreal:+.2f} pts" if unreal is not None else "—",
                    delta=f"{unreal:+.2f}" if unreal is not None else None,
                )
                c4.metric("To Target", f"{to_target:.1f} pts" if to_target is not None else "—")
                c5.metric("To SL",     f"{to_sl:.1f} pts"     if to_sl     is not None else "—")

                # ── Manual close ──────────────────────────────────────────
                _confirm_key = f"_confirm_close_{t['id']}"
                if st.session_state.get(_confirm_key):
                    st.warning("⚠️ Confirm manual close — this will place a SELL order on Kite and close the trade.")
                    if config.load_settings().get("MODE") == "live":
                        st.info("🔴 LIVE MODE — system will place the SELL order on Kite automatically.")
                    _ca, _cb, _cc = st.columns([1, 1, 2])
                    if _ca.button("✅ Yes, close now", type="primary",
                                  use_container_width=True, key=f"close_yes_{t['id']}"):
                        _exit = ltp or entry
                        _pnl  = round((_exit - entry if zone_class == "demand" else entry - _exit), 2)
                        # ── Live: place real options exit order ───────────
                        if config.load_settings().get("MODE") == "live":
                            _opts_sym = t.get("options_symbol")
                            if _opts_sym:
                                try:
                                    from brokers.kite_adapter import KiteAdapter as _KA2
                                    from journal.db import update_signal_exit_order as _useo
                                    _ka2 = _KA2()
                                    # BUG 1 fix: use stored lot size from entry, not live get_lot_size()
                                    _qty2 = t.get("options_lot_size") or _ka2.get_lot_size()
                                    _sell_oid = _ka2.place_options_order(_opts_sym, "SELL", _qty2)
                                    st.toast(f"LIVE EXIT: SELL {_opts_sym} → order #{_sell_oid}", icon="🔴")
                                    import time as _t2; _t2.sleep(3)
                                    _sell_fill = _ka2.get_order_fill_price(_sell_oid)
                                    if _sell_fill > 0:
                                        _useo(t["id"], _sell_oid, _sell_fill)
                                except Exception as _e2:
                                    st.error(f"⚠️ Exit order failed: {_e2}\nClose manually on Kite!")
                            else:
                                st.warning("No options symbol stored — close position manually on Kite.")
                        close_trade(t["id"], _exit, "manual", closed_by="dashboard")
                        import notify as _n
                        _n.trade_closed(t["id"], _exit, "manual", _pnl)
                        st.session_state.pop(_confirm_key, None)
                        st.toast(f"Trade #{t['id']} closed manually at {_exit:.2f}", icon="✅")
                        st.rerun()
                    if _cb.button("Cancel", use_container_width=True, key=f"close_no_{t['id']}"):
                        st.session_state.pop(_confirm_key, None)
                        st.rerun()
                else:
                    if st.button("🚨 Close Trade Now", use_container_width=True,
                                 key=f"close_btn_{t['id']}",
                                 help="Manually exit this trade at current LTP"):
                        st.session_state[_confirm_key] = True
                        st.rerun()

        if ltp is None:
            st.warning("Could not fetch live LTP — token may be expired.")
        st.divider()

    # ── Pending Approvals ─────────────────────────────────────────────────
    st.header("Pending Approvals")
    st.caption(
        f"Mode: **{config.MODE.upper()}** — "
        + ("Approving will log the trade. No real order is placed in paper mode."
           if config.MODE == "paper"
           else "Approving will place a LIVE order on Kite.")
    )

    pending_rows = get_pending_signals()

    if pending_rows:
        if st.button(f"🗑 Reject All {len(pending_rows)} Pending", type="secondary"):
            try:
                n = reject_all_pending()
                st.session_state["reject_msg"] = f"✅ Rejected {n} signals — data kept for analysis."
            except Exception as e:
                st.session_state["reject_msg"] = f"❌ Error: {e}"
            st.rerun()

    if "reject_msg" in st.session_state:
        st.success(st.session_state.pop("reject_msg"))

    if pending_rows:
        st.divider()

    if not pending_rows:
        st.success("✅ No pending signals — all caught up.")
    elif open_trades:
        st.warning(
            f"⚠️ Trade #{open_trades[0]['id']} is still active. "
            "Wait for it to close before approving a new one."
        )
        st.info(f"{len(pending_rows)} signal(s) queued — available once active trade closes.")
    else:
        for row in pending_rows:
            r = dict(row)
            zone_label = (
                f"🟢 DEMAND {r['zone_type']}"
                if r["zone_class"] == "demand"
                else f"🔴 SUPPLY {r['zone_type']}"
            )
            with st.container(border=True):
                st.subheader(f"Signal #{r['id']} — {zone_label} | {r['timeframe']}")

                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric("Entry",     f"{r['entry']:.2f}")
                m2.metric("Stop Loss", f"{r['stop_loss']:.2f}")
                m3.metric("Target",    f"{r['intraday_target']:.2f}")
                m4.metric("Score",     f"{r['booster_score']:.1f}/10")
                m5.metric("Type",      f"Type {r['entry_type']}")

                risk_pts = abs(r["entry"] - r["stop_loss"])
                rr = abs(r["intraday_target"] - r["entry"]) / risk_pts if risk_pts else 0
                st.caption(
                    f"Risk: **{risk_pts:.1f} pts** | R:R 1:{rr:.1f} | "
                    f"Confluence: {r.get('confluence_tfs') or '—'} | "
                    f"Position size: {r.get('position_size', '—')}"
                )

                ba, br, _ = st.columns([1, 1, 2])
                if ba.button("✅ Approve", type="primary", use_container_width=True, key=f"app_{r['id']}"):
                    approve_signal(r["id"])
                    import notify
                    # ── Live: place real options entry order ──────────────
                    if config.load_settings().get("MODE") == "live":
                        try:
                            from brokers.kite_adapter import KiteAdapter as _KA
                            from journal.db import update_signal_order
                            _k   = _KA()
                            if not _k._token_loaded:
                                st.toast("⚠️ No Kite token — approval saved but NO real order placed. Go to Engine tab and save today's token first.", icon="⚠️")
                            else:
                                _k.validate_entry(r["entry"], r["stop_loss"], r["zone_class"])
                                _contract = _k.get_options_contract(r["entry"], r["zone_class"])
                                _qty      = _contract["lot_size"]
                                _oid      = _k.place_options_order(_contract["symbol"], "BUY", _qty)
                                update_signal_order(r["id"], _oid, _contract["symbol"], _qty)
                                st.toast(
                                    f"🔴 LIVE ORDER PLACED: BUY {_contract['symbol']} × {_qty} lots | "
                                    f"Order #{_oid}", icon="🔴"
                                )
                                # Fetch actual fill price (wait for order to complete)
                                import time as _t; _t.sleep(3)
                                _fill = _k.get_order_fill_price(_oid)
                                if _fill > 0:
                                    from journal.db import update_signal_entry_price as _uep
                                    _uep(r["id"], _fill)
                                    st.toast(f"Entry premium recorded: ₹{_fill:.2f}", icon="📋")
                        except Exception as _e:
                            from journal.db import reject_signal
                            reject_signal(r["id"], f"Order failed: {_e}")
                            st.toast(f"❌ Order failed — signal #{r['id']} auto-rejected. Approve next signal.\n{_e}", icon="❌")
                            st.rerun()
                            st.stop()
                    notify.trade_approved(r["id"], r["entry"], r["stop_loss"], r["intraday_target"])
                    st.toast(f"Signal #{r['id']} approved — trade is active.", icon="✅")
                    st.rerun()
                if br.button("❌ Reject", use_container_width=True, key=f"rej_{r['id']}"):
                    reject_signal(r["id"])
                    st.toast(f"Signal #{r['id']} rejected.", icon="❌")
                    st.rerun()


with tab_approvals:
    _approvals_tab()

# ══════════════════════════════════════════════════════════════════════════
# TAB 3 — SIGNALS
# ══════════════════════════════════════════════════════════════════════════
with tab_signals:
    st.header("Signals")
    _sf_c1, _sf_c2 = st.columns([2, 1])
    selected_date = _sf_c1.date_input("Date", value=date.today())
    _sig_mode_filter = _sf_c2.radio(
        "Mode", ["All", "Paper", "Live"], horizontal=True, key="sig_mode_filter",
        label_visibility="collapsed",
    )
    rows = get_signals_for_date(selected_date.isoformat())

    if not rows:
        st.info("No signals logged for this date.")
    else:
        df = pd.DataFrame([dict(r) for r in rows])
        if "mode" not in df.columns:
            df["mode"] = "paper"

        if _sig_mode_filter == "Paper":
            df = df[df["mode"] == "paper"]
        elif _sig_mode_filter == "Live":
            df = df[df["mode"] == "live"]

        # Compute actual options ₹ P&L where both entry and exit premium are known
        if "options_entry_price" in df.columns and "options_exit_price" in df.columns:
            lot = df.get("options_lot_size", 65).fillna(65).astype(int)
            entry_p = pd.to_numeric(df["options_entry_price"], errors="coerce")
            exit_p  = pd.to_numeric(df["options_exit_price"],  errors="coerce")
            df["options_pnl_rs"] = ((exit_p - entry_p) * lot).round(2)

        display_cols = [
            "id", "mode", "status", "date", "time_signal", "zone_type", "zone_class", "timeframe",
            "entry", "stop_loss", "intraday_target",
            "booster_score", "confluence_count", "confluence_tfs",
            "entry_type", "position_size",
            "exit_price", "exit_reason", "pnl_points", "result",
            "options_entry_price", "options_exit_price", "options_pnl_rs",
            "kite_order_id", "options_symbol",
        ]
        display_cols = [c for c in display_cols if c in df.columns]

        if df.empty:
            st.info(f"No {_sig_mode_filter.lower()} signals for this date.")
        else:
            def _colour_row(row):
                styles = [""] * len(row)
                idx = row.index.tolist()
                if "result" in idx:
                    v = row["result"]
                    if v == "win":  styles[idx.index("result")] = "background-color:#d4edda;color:#155724"
                    if v == "loss": styles[idx.index("result")] = "background-color:#f8d7da;color:#721c24"
                if "mode" in idx:
                    m = row["mode"]
                    if m == "live":  styles[idx.index("mode")] = "background-color:#f8d7da;color:#721c24;font-weight:bold"
                    if m == "paper": styles[idx.index("mode")] = "background-color:#d1ecf1;color:#0c5460"
                return styles

            styled = df[display_cols].style.apply(_colour_row, axis=1)
            st.dataframe(styled, use_container_width=True, hide_index=True)

        st.divider()

        # ── Close trade ───────────────────────────────────────────────────
        open_df = (
            df[(df["exit_price"].isna()) & (df["status"] == "approved")]
            if "exit_price" in df.columns and "status" in df.columns
            else pd.DataFrame()
        )

        if not open_df.empty:
            st.subheader("Close a Trade")
            options = {
                f"#{r['id']} | {r['zone_class'].upper()} {r['zone_type']} | "
                f"Entry {r['entry']} → TGT {r['intraday_target']}": r["id"]
                for _, r in open_df.iterrows()
            }
            label    = st.selectbox("Select open trade", list(options.keys()))
            trade_id = options[label]

            with st.form("close_form"):
                c1, c2 = st.columns(2)
                exit_price  = c1.number_input("Exit price", min_value=0.0, step=0.05, format="%.2f")
                exit_reason = c2.selectbox("Reason", ["target", "stoploss", "manual", "eod"])
                notes       = st.text_input("Notes (optional)")
                if st.form_submit_button("✅ Close Trade"):
                    if exit_price == 0:
                        st.error("Enter a valid exit price.")
                    else:
                        close_trade(trade_id, exit_price, exit_reason, notes, closed_by="dashboard")
                        st.success(f"Trade #{trade_id} closed at {exit_price}.")
                        st.rerun()
        else:
            st.success("No open trades for this date.")

        st.divider()

        # ── Export ────────────────────────────────────────────────────────
        if not df.empty:
            c1, c2 = st.columns(2)
            if c1.button("💾 Save CSV to disk"):
                path = export_day(selected_date.isoformat())
                c1.success(f"Saved → {path}")

            c2.download_button(
                "⬇ Download CSV",
                data=df[display_cols].to_csv(index=False).encode("utf-8"),
                file_name=f"trades_{selected_date}.csv",
                mime="text/csv",
            )

# ══════════════════════════════════════════════════════════════════════════
# TAB 4 — PERFORMANCE
# ══════════════════════════════════════════════════════════════════════════
with tab_performance:
    _perf_mode = st.radio(
        "View", ["All", "📄 Paper", "🔴 Live"], horizontal=True, key="perf_mode_filter",
        label_visibility="collapsed",
    )
    _perf_mode_label = {"All": "All Trades", "📄 Paper": "Paper Trades", "🔴 Live": "Live Trades"}[_perf_mode]
    st.header(f"Performance — {_perf_mode_label}")

    try:
        con = sqlite3.connect(config.DB_PATH)
        all_df = pd.read_sql("SELECT * FROM signals WHERE result IS NOT NULL", con)
        con.close()

        if "mode" not in all_df.columns:
            all_df["mode"] = "paper"

        if _perf_mode == "📄 Paper":
            all_df = all_df[all_df["mode"] == "paper"].copy()
        elif _perf_mode == "🔴 Live":
            all_df = all_df[all_df["mode"] == "live"].copy()

        if all_df.empty:
            st.info(f"No closed {_perf_mode_label.lower()} yet.")
        else:
            all_df["pnl_points"] = pd.to_numeric(all_df["pnl_points"], errors="coerce").fillna(0)
            all_df["date"]       = pd.to_datetime(all_df["date"])

            total     = len(all_df)
            wins      = (all_df["result"] == "win").sum()
            losses    = (all_df["result"] == "loss").sum()
            win_rate  = wins / total * 100 if total else 0
            total_pnl = all_df["pnl_points"].sum()
            avg_win   = all_df.loc[all_df["result"] == "win",  "pnl_points"].mean() if wins  else 0
            avg_loss  = all_df.loc[all_df["result"] == "loss", "pnl_points"].mean() if losses else 0
            profit_factor = abs(avg_win / avg_loss) if avg_loss else float("inf")
            best_trade  = all_df["pnl_points"].max()
            worst_trade = all_df["pnl_points"].min()

            # ── Summary metrics ───────────────────────────────────────────
            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Total Trades", total)
            m2.metric("Win Rate",     f"{win_rate:.1f}%")
            m3.metric("Total P&L",    f"{total_pnl:.2f} pts")
            m4.metric("Avg Win",      f"+{avg_win:.2f} pts")
            m5.metric("Avg Loss",     f"{avg_loss:.2f} pts")

            m6, m7, m8, m9, m10 = st.columns(5)
            m6.metric("Wins",          int(wins))
            m7.metric("Losses",        int(losses))
            m8.metric("Profit Factor", f"{profit_factor:.2f}" if profit_factor != float("inf") else "∞")
            m9.metric("Best Trade",    f"+{best_trade:.2f} pts")
            m10.metric("Worst Trade",  f"{worst_trade:.2f} pts")

            st.divider()

            # ── Cumulative P&L curve ──────────────────────────────────────
            st.subheader("Cumulative P&L")
            cum_df = all_df.sort_values("date")[["date", "pnl_points"]].copy()
            cum_df["cumulative"] = cum_df["pnl_points"].cumsum()
            st.line_chart(cum_df.set_index("date")["cumulative"], use_container_width=True)

            st.divider()

            # ── Daily P&L bar chart ───────────────────────────────────────
            st.subheader("Daily P&L")
            daily_df = all_df.groupby(all_df["date"].dt.date)["pnl_points"].sum().reset_index()
            daily_df.columns = ["date", "P&L"]
            st.bar_chart(daily_df.set_index("date"), use_container_width=True)

            st.divider()

            # ── Breakdown ─────────────────────────────────────────────────
            st.subheader("Breakdown")

            def _wr_style(val):
                if isinstance(val, float):
                    if val >= 50:  return "color: #2ecc71; font-weight: bold"
                    if val < 35:   return "color: #e74c3c; font-weight: bold"
                return ""

            col_left, col_right = st.columns(2)

            with col_left:
                st.caption("By Zone Type")
                zone_df = all_df.groupby("zone_type")["pnl_points"].agg(
                    Trades="count", Total_PnL="sum",
                    Win_Rate=lambda x: round((x > 0).mean() * 100, 1)
                ).reset_index().sort_values("Win_Rate", ascending=False)
                st.dataframe(
                    zone_df.style.map(_wr_style, subset=["Win_Rate"]),
                    use_container_width=True, hide_index=True,
                )

            with col_right:
                st.caption("By Timeframe")
                tf_df = all_df.groupby("timeframe")["pnl_points"].agg(
                    Trades="count", Total_PnL="sum",
                    Win_Rate=lambda x: round((x > 0).mean() * 100, 1)
                ).reset_index().sort_values("Win_Rate", ascending=False)
                st.dataframe(
                    tf_df.style.map(_wr_style, subset=["Win_Rate"]),
                    use_container_width=True, hide_index=True,
                )

            st.caption("By Zone Class")
            class_df = all_df.groupby("zone_class")["pnl_points"].agg(
                Trades="count", Total_PnL="sum",
                Win_Rate=lambda x: round((x > 0).mean() * 100, 1)
            ).reset_index().sort_values("Win_Rate", ascending=False)
            st.dataframe(
                class_df.style.map(_wr_style, subset=["Win_Rate"]),
                use_container_width=True, hide_index=True,
            )

            st.divider()

            # ── System Recommendation ──────────────────────────────────────
            st.subheader("System Recommendation")
            MIN_TRADES_FOR_REC = 3

            good_zone_types  = zone_df.loc[
                (zone_df["Win_Rate"] >= 50) & (zone_df["Trades"] >= MIN_TRADES_FOR_REC),
                "zone_type"
            ].tolist()
            weak_zone_types  = zone_df.loc[
                (zone_df["Win_Rate"] < 35)  & (zone_df["Trades"] >= MIN_TRADES_FOR_REC),
                "zone_type"
            ].tolist()
            good_tfs = tf_df.loc[
                (tf_df["Win_Rate"] >= 50) & (tf_df["Trades"] >= MIN_TRADES_FOR_REC),
                "timeframe"
            ].tolist()
            good_classes = class_df.loc[
                (class_df["Win_Rate"] >= 50) & (class_df["Trades"] >= MIN_TRADES_FOR_REC),
                "zone_class"
            ].tolist()

            if total < MIN_TRADES_FOR_REC:
                st.info(
                    f"Need at least {MIN_TRADES_FOR_REC} closed trades for recommendations "
                    f"({total} so far). Keep trading!"
                )
            else:
                rec_lines = []
                if good_tfs:
                    rec_lines.append(f"- **Timeframe:** {' / '.join(good_tfs)}")
                if good_classes:
                    rec_lines.append(f"- **Zone class:** {' + '.join(good_classes)} only")
                if good_zone_types:
                    rec_lines.append(f"- **Zone types:** {', '.join(good_zone_types)}")

                if rec_lines:
                    st.success(
                        "Based on your trade history, the system recommends:\n\n"
                        + "\n".join(rec_lines)
                    )
                else:
                    st.info("No zone type or timeframe has reached ≥50% win rate yet. "
                            "More data needed — keep trading.")

                for zt in weak_zone_types:
                    wr = zone_df.loc[zone_df["zone_type"] == zt, "Win_Rate"].values[0]
                    n  = zone_df.loc[zone_df["zone_type"] == zt, "Trades"].values[0]
                    st.warning(
                        f"⚠️ **{zt}** has {wr:.0f}% win rate over {n} trades — "
                        f"consider disabling in Settings → Zone Classes."
                    )

            st.divider()

            st.divider()

            # ── Validation checklist ──────────────────────────────────────
            st.subheader("Validation Checklist (before going live)")
            st.caption("Always evaluated on **paper trades only** — independent of the mode filter above.")
            _chk_con = sqlite3.connect(config.DB_PATH)
            _paper_df_chk = pd.read_sql(
                "SELECT pnl_points, result FROM signals WHERE result IS NOT NULL AND mode='paper'",
                _chk_con,
            )
            _chk_con.close()
            avg_pnl = _paper_df_chk["pnl_points"].mean() if not _paper_df_chk.empty else 0
            total_chk = len(_paper_df_chk)
            wins_chk  = (_paper_df_chk["result"] == "win").sum() if not _paper_df_chk.empty else 0
            win_rate_chk = wins_chk / total_chk * 100 if total_chk else 0

            # Count distinct weekday dates with ANY signal (any status) in last 14 days.
            # Using all signals (not just closed) so holidays/weekends are naturally skipped.
            from datetime import date as _date, timedelta as _td
            _con2 = sqlite3.connect(config.DB_PATH)
            _active_dates = _con2.execute("SELECT DISTINCT date FROM signals").fetchall()
            _con2.close()
            _cutoff = _date.today() - _td(days=14)
            _trading_days = set()
            for (_d,) in _active_dates:
                try:
                    _day = _date.fromisoformat(_d)
                    if _day >= _cutoff and _day.weekday() < 5:
                        _trading_days.add(_day)
                except Exception:
                    pass
            _active_day_count = len(_trading_days)
            _five_days_ok = _active_day_count >= 5

            st.checkbox(f"20+ paper trades logged ({total_chk} so far)",   value=total_chk >= 20)
            st.checkbox(f"Paper win rate > 50% ({win_rate_chk:.1f}%)",     value=win_rate_chk > 50)
            st.checkbox(f"Paper avg P&L positive ({avg_pnl:.2f} pts)", value=avg_pnl > 0)
            st.checkbox("System detects zones correctly",         value=False)
            st.checkbox(
                f"No crashes for 5 consecutive trading days ({_active_day_count} active days in last 2 weeks)",
                value=_five_days_ok,
            )

            st.divider()

            # ── Raw trade log ─────────────────────────────────────────────
            with st.expander("📋 All Closed Trades"):
                st.dataframe(all_df, use_container_width=True, hide_index=True)

    except Exception as e:
        st.warning(f"Could not load performance data: {e}")


# ══════════════════════════════════════════════════════════════════════════
# TAB 5 — LEARNING
# ══════════════════════════════════════════════════════════════════════════
with tab_learning:
    _learn_mode = st.radio(
        "Analyse", ["All", "📄 Paper", "🔴 Live"], horizontal=True, key="learn_mode_filter",
        label_visibility="collapsed",
    )
    _learn_mode_sql = {"All": None, "📄 Paper": "paper", "🔴 Live": "live"}[_learn_mode]
    # BUG 12 fix: build WHERE clause with parameterized queries instead of string interpolation
    if _learn_mode_sql:
        _learn_where_sql  = "result IS NOT NULL AND mode = ?"
        _learn_where_args = (_learn_mode_sql,)
    else:
        _learn_where_sql  = "result IS NOT NULL"
        _learn_where_args = ()
    st.header(f"🤖 What the System Has Learned — {'All Trades' if not _learn_mode_sql else _learn_mode_sql.title()}")
    st.caption(
        "Auto-learn runs after every 10 closed trades. "
        "It disables zone types or timeframes whose win rate drops below 35% over 10+ trades."
    )

    _l = config.load_settings()
    _disabled_zt  = _l.get("DISABLED_ZONE_TYPES", [])
    _all_tfs_l    = [config.TF_LOWER, config.TF_INTERMEDIATE, config.TF_HIGHER]
    _active_tfs_l = _l.get("SCAN_TIMEFRAMES", _all_tfs_l)
    _disabled_tfs = [tf for tf in _all_tfs_l if tf not in _active_tfs_l]

    # ── Win-rate table by zone type ───────────────────────────────────────
    st.subheader("Zone Type Performance")
    try:
        _con_l = sqlite3.connect(config.DB_PATH)
        _rows_l = _con_l.execute(
            f"SELECT zone_type, pnl_points FROM signals WHERE {_learn_where_sql}",
            _learn_where_args,
        ).fetchall()
        _con_l.close()

        if _rows_l:
            import collections
            _zt_stats: dict = collections.defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
            for zt, pnl in _rows_l:
                _zt_stats[zt]["trades"] += 1
                if pnl and pnl > 0:
                    _zt_stats[zt]["wins"] += 1
                _zt_stats[zt]["pnl"] += pnl or 0.0

            _zt_rows = []
            for zt, s in sorted(_zt_stats.items()):
                wr = s["wins"] / s["trades"] * 100 if s["trades"] else 0
                status = "❌ Disabled" if zt in _disabled_zt else ("✅ Active" if wr >= 35 else "⚠️ At risk")
                _zt_rows.append({
                    "Zone Type": zt,
                    "Trades": s["trades"],
                    "Wins": s["wins"],
                    "Win Rate": f"{wr:.0f}%",
                    "Avg P&L (pts)": f"{s['pnl'] / s['trades']:.2f}",
                    "Status": status,
                })
            st.dataframe(_zt_rows, use_container_width=True, hide_index=True)
        else:
            st.info("No closed trades yet — learning will begin after 10 trades.")
    except Exception as e:
        st.warning(f"Could not load zone stats: {e}")

    st.divider()

    # ── Win-rate table by timeframe ───────────────────────────────────────
    st.subheader("Timeframe Performance")
    try:
        _con_l2 = sqlite3.connect(config.DB_PATH)
        _rows_l2 = _con_l2.execute(
            f"SELECT timeframe, pnl_points FROM signals WHERE {_learn_where_sql}",
            _learn_where_args,
        ).fetchall()
        _con_l2.close()

        if _rows_l2:
            import collections  # BUG 7 fix: import locally so it's always in scope regardless of zone type block above
            _tf_stats: dict = collections.defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
            for tf, pnl in _rows_l2:
                _tf_stats[tf]["trades"] += 1
                if pnl and pnl > 0:
                    _tf_stats[tf]["wins"] += 1
                _tf_stats[tf]["pnl"] += pnl or 0.0

            _tf_rows = []
            for tf, s in sorted(_tf_stats.items()):
                wr = s["wins"] / s["trades"] * 100 if s["trades"] else 0
                status = "❌ Disabled" if tf in _disabled_tfs else ("✅ Active" if wr >= 35 else "⚠️ At risk")
                _tf_rows.append({
                    "Timeframe": tf,
                    "Trades": s["trades"],
                    "Wins": s["wins"],
                    "Win Rate": f"{wr:.0f}%",
                    "Avg P&L (pts)": f"{s['pnl'] / s['trades']:.2f}",
                    "Status": status,
                })
            st.dataframe(_tf_rows, use_container_width=True, hide_index=True)
        else:
            st.info("No closed trades yet.")
    except Exception as e:
        st.warning(f"Could not load timeframe stats: {e}")

    st.divider()

    # ── Re-enable controls ────────────────────────────────────────────────
    st.subheader("Re-enable Disabled Items")
    if not _disabled_zt and not _disabled_tfs:
        st.success("Nothing is currently disabled by the learning engine.")
    else:
        st.caption("Re-enabling restores the item to active scanning. Monitor results carefully after re-enabling.")
        for zt in _disabled_zt:
            c1, c2 = st.columns([3, 1])
            c1.markdown(f"❌ Zone type **{zt}**")
            if c2.button(f"Re-enable", key=f"l_reenable_zt_{zt}", use_container_width=True):
                config.save_settings({"DISABLED_ZONE_TYPES": [z for z in _disabled_zt if z != zt]})
                st.success(f"{zt} re-enabled.")
                st.rerun()

        for tf in _disabled_tfs:
            c1, c2 = st.columns([3, 1])
            c1.markdown(f"❌ Timeframe **{tf}**")
            if c2.button(f"Re-enable", key=f"l_reenable_tf_{tf}", use_container_width=True):
                config.save_settings({"SCAN_TIMEFRAMES": _active_tfs_l + [tf]})
                st.success(f"{tf} re-enabled.")
                st.rerun()

    st.divider()

    # ── Time of Day Analysis ──────────────────────────────────────────────
    st.subheader("Time of Day Analysis")
    st.caption("Which hours produce the best results?")
    try:
        import collections as _col
        _con_t = sqlite3.connect(config.DB_PATH)
        _rows_t = _con_t.execute(
            f"SELECT time_signal, pnl_points, result FROM signals WHERE {_learn_where_sql}",
            _learn_where_args,
        ).fetchall()
        _con_t.close()

        if _rows_t:
            _hr_stats: dict = _col.defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
            for _ts, _pnl, _res in _rows_t:
                try:
                    _hr = int(_ts[:2])
                except Exception:
                    continue
                _hr_stats[_hr]["trades"] += 1
                if _res == "win":
                    _hr_stats[_hr]["wins"] += 1
                _hr_stats[_hr]["pnl"] += _pnl or 0.0

            _hr_rows = []
            _best_hr = max(_hr_stats, key=lambda h: _hr_stats[h]["pnl"] / _hr_stats[h]["trades"])
            _worst_hr = min(
                (h for h in _hr_stats if _hr_stats[h]["trades"] >= 3),
                key=lambda h: _hr_stats[h]["wins"] / _hr_stats[h]["trades"],
                default=None,
            )
            for _hr in sorted(_hr_stats):
                s = _hr_stats[_hr]
                wr = s["wins"] / s["trades"] * 100
                avg = s["pnl"] / s["trades"]
                tag = ""
                if _hr == _best_hr:
                    tag = "⭐ Best"
                elif _worst_hr and _hr == _worst_hr:
                    tag = "⚠️ Weakest"
                _hr_rows.append({
                    "Hour": f"{_hr:02d}:00 – {_hr:02d}:59",
                    "Trades": s["trades"],
                    "Wins": s["wins"],
                    "Losses": s["trades"] - s["wins"],
                    "Win Rate": f"{wr:.0f}%",
                    "Avg P&L (pts)": f"{avg:+.1f}",
                    "Total P&L (pts)": f"{s['pnl']:+.1f}",
                    "Note": tag,
                })
            st.dataframe(_hr_rows, use_container_width=True, hide_index=True)

            if _worst_hr:
                st.info(
                    f"💡 **{_worst_hr:02d}:00–{_worst_hr:02d}:59** is your weakest window "
                    f"({_hr_stats[_worst_hr]['wins']}/{_hr_stats[_worst_hr]['trades']} wins). "
                    f"Consider setting Scan Window in Settings to skip it."
                )
        else:
            st.info("No closed trades yet.")
    except Exception as e:
        st.warning(f"Could not load time analysis: {e}")

    st.divider()

    # ── Trading Lessons from Live Sessions ───────────────────────────────
    st.subheader("📚 Trading Lessons from Live Sessions")
    st.caption("Key patterns and rules extracted from real trades. Updated after each live session.")

    with st.expander("Aug 29, 2026 — 3-year backtest: the edge does not hold as traded", expanded=True):
        st.markdown("""
Built an offline backtest (`backtest/`) that replays the **real** `engine/` zone logic against
**3 years of real Kite 5-min Nifty + VIX data**. Futures P&L uses real index data. Full detail:
`backtest/README.md` and `AGENT_KNOWLEDGE.md` §14.

**1. Approving from Telegram is the biggest single leak (~4,300 index pts / 3 years).**

| Entry method | idx pts / 3y | per trade | "target" hit at | losing "target" exits |
|---|---|---|---|---|
| Limit order resting AT the proximal | **+366** | +1.1 | **+41.8 pts** | **0 of 84** |
| Market order ~2.5 min after signal | **−3,941** | −4.9 | +0.8 pts | 267 of 543 |
| Market order ~7.5 min after signal | −4,624 | −5.8 | −0.5 pts | 259 of 508 |

The few *seconds* barely matter — it's the 2–5 minutes between the signal and the tap. By then
price has run to the target, so "target" fills instantly at a level barely above your (worse)
entry and **half of those "wins" are losses**. Fix: `docs/LIMIT_ENTRY_DESIGN.md` — a real limit
order that rests at the proximal and fills only if price comes back.
**Until then: approve the instant the signal arrives, or not at all.**

**2. Demand / CE has negative expectancy over 3 years.**

| Side | idx pts / 3y | per trade |
|---|---|---|
| supply / PE | **+611** (245 trades) | +2.5 |
| demand / CE | **−245** (88 trades) | −2.8 |

**3. Costs exceed the edge.** PE side, correct entry: gross **+₹133/trade**. Zerodha futures
round-trip ≈ **₹493/trade** (STT alone ≈ ₹312, charged on ₹15.6 L notional). Net **−₹360/trade**.
Naked options are worse — every strike/expiry variant loses ₹950–1,400/trade.

**4. Not regime-stable.**

| Year | idx pts | PE | CE | win% |
|---|---|---|---|---|
| 2024 | **+641** | +688 | −47 | 45 |
| 2025 | +150 | +50 | +100 | 41 |
| 2026 | **−386** | −96 | −290 | 34 |

2024 carried the whole result. 2026 — the live-trading year — is net negative.

**Config change deployed Aug 29** — high-quality-only, PE-only:

| Setting | Old | New | Why |
|---|---|---|---|
| Zone Classes | demand + supply | **supply only** | CE = −245 pts / 3y |
| Min Booster Score | 8 | **10** | perfect setups only |
| Min Confluence | 2 | **3** | all 3 TFs must agree — this is what actually concentrates quality |
| Zone Approach | 50 | **30** | tighter = enter closer to the line |
| Min Risk (new) | — | **15 pts** | skip degenerate tiny-base zones |
| Max Trades/Day | 4 | **2** | cap (real frequency at this bar ≈ 1/month) |

Backtest of the strict config (PE, score 10, conf 3, risk 15, approach 30): **42 trades in 3 years,
+3.5 index pts/trade, ~40% win**. Still **−₹308/trade after costs** — the booster score is a weak
quality filter, confluence 3 + tight approach + min-risk is what helps. This buys time and cuts
exposure; the limit-entry fix is what changes the economics.
        """)

    with st.expander("Aug 28, 2026 — Manual close = ₹1,398 loss. Options SL added. Profit-chasing strategy."):
        st.markdown("""
**4 trades. +113 index pts (4/4 wins). -₹1,333 options. Plus -₹578 from 2 manual Kite trades.**

| Trade | Zone | Hold | Index P&L | Options P&L | Why |
|-------|------|------|-----------|-------------|-----|
| 856 AUTO | RBD supply | 1 min | +28.1 pts ✅ | **-₹104** | Speed crush — 28 pts not enough |
| 862 | DBD supply | 1 min | +50.1 pts ✅ | **+₹172** | 50 pts overcame IV crush ✅ |
| 867 | DBD supply | 13 min | +2.3 pts ✅ | **-₹1,398** | **MANUAL CLOSE during stall** |
| 868 | DBD supply | 15 sec | +32.6 pts ✅ | **-₹3** | Too fast, essentially zero |

**Trade 867 anatomy — the ₹1,398 loss:**
Nifty stalled for 13 minutes (+2.3 pts only). Theta ate premium: ₹113.35 → ₹91.85 = -₹21.50 × 65. Target was still 43 pts away. User manually closed. If left to system: target or SL would have closed at a fraction of that loss.

**Speed-of-move threshold confirmed:**
| Move | Time | Options |
|------|------|---------|
| 28 pts | 1 min | LOSE |
| 50 pts | 1 min | WIN ✅ |
| 32 pts | 15 sec | FLAT |

**Key rules from Aug 28:**
1. **Never manually close a trade — ever.** Manual close locks in maximum theta decay at the worst moment.
2. **Never trade on Kite outside the bot.** System context is invisible — both manual Kite trades lost.
3. **Options need a loss cut, not just a profit lock.** When Nifty stalls, theta bleeds premium with no protection.

**Fix deployed:** `OPTIONS_SL_PCT=20` — if options premium drops 20% from entry, system closes immediately. Stops the #867 scenario automatically.

**9 skipped signals all hit target (+345 pts).** Zone detection is working. Losses came from execution, not signals.
        """)

    with st.expander("Aug 27, 2026 — Speed-of-move IV crush. 3/3 index wins, 2/3 options losses."):
        st.markdown("""
**3 trades. +226 index pts (3/3 wins). +₹373 options net.**

| Trade | Hold | Index P&L | Options P&L |
|-------|------|-----------|-------------|
| 849 | **6 min** | +74.4 pts | **+₹955** ✅ |
| 851 | **37 sec** | +77.2 pts | **-₹426** ❌ |
| 855 | **39 sec** | +74.6 pts | **-₹156** ❌ |

**The phenomenon:** When Nifty drops 77 pts in 37 seconds, market makers instantly price out all fear premium — time value collapses faster than intrinsic accumulates. Same direction, same zone quality, opposite hold time = opposite options outcome.

**Trade 849 was the only profitable one because it took 6 minutes.** At 37 seconds, intrinsic gained 77 pts but time value lost 84 pts.

**Why 851 and 855 were signalled (they weren't errors):** Each is a different zone at a different proximal level. Nifty kept falling and hitting fresh supply zones. All scored 10/10. The system was correct on direction. The problem was no "day is done" stop after first win.

**Fix deployed:** `DAILY_OPTIONS_TARGET=730` stops new signals after ₹730 options profit. Also `TIME_EXIT_HOUR=13` now blocks new signals (not just closes open trades) — trade 855 at 14:01 would have been blocked.
        """)

    with st.expander("Aug 26, 2026 — First positive CE options P&L. AUTO-FIRST works. IV Rank live."):
        st.markdown("""
**2 signals. 1 traded (AUTO-FIRST). +58 index pts. +₹477 options.**

Signal 835 auto-executed at market open, hit target in 9 minutes. First-ever positive CE options P&L.

**Why it worked:** IV Rank was 8% (cheapest in 52-week range) at signal time. Cheap IV + correct direction + 9-minute hold = options and index aligned.

**IV Rank filter confirmed live:** Signal 844 Telegram showed `IV Rank: 🟢 8%` — filter working correctly.

**Rule:** Low IV Rank (< 30%) = strong condition to buy options. High IV Rank = premium likely to crush even on correct direction.
        """)

    with st.expander("Aug 25, 2026 — ITM strike + 8 DTE: -₹29 options. PE IV crush confirmed."):
        st.markdown("""
**1 trade (signal 824): DBD supply, 3 TF confluence. +77.2 index pts. Options P&L: -₹29.**

| What | Detail |
|------|--------|
| Strike | 24350 PE — 1 strike ITM (ATM was 24300) ✅ |
| Expiry | Sep 2 (8 DTE — jumped past Aug 26 per 2-day rule) ✅ |
| Options P&L | -₹29 vs -₹448 to -₹1,544 on Aug 24 — **98% improvement** |
| Auto-expiry | 33 of 34 signals auto-expired — only highest quality (3 TF) approved |

**Why -₹29 despite +77 index pts?**
PE IV crush: Fear was already priced into the premium at entry (high IV at 09:59). When Nifty fell 77 pts, IV simultaneously collapsed as the feared move materialised. Delta gain ≈ IV loss → net flat.

**Key rule:** PE IV crush happens when VIX is already elevated at entry AND the move happens fast. Fix: check IV Rank before approving — buy only when IV is historically cheap.

**Comparison:**
| Day | Strike | DTE | Options P&L |
|-----|--------|-----|-------------|
| Aug 24 | ATM CE | 1 day | -₹448 to -₹1,544 |
| Aug 25 | 1-ITM PE | 8 days | **-₹29** |
        """)

    with st.expander("Aug 24, 2026 — Won points, lost money. CE IV crush + breakeven SL trap."):
        st.markdown("""
**4 trades. +210 index pts total. -₹1,884 options total.**

| Trade | Index P&L | Options P&L | Why |
|-------|-----------|-------------|-----|
| 801 CE | +54.3 pts | -₹448 | IV crush on morning recovery |
| 806 CE | 0 pts (breakeven SL) | **-₹1,544** | SL protected index, not options |
| 809 CE | +78.8 pts | -₹97 | Less IV crush — ITM strike |
| 810 PE | +77.2 pts | +₹205 | Afternoon reversal — IV expanded |

**Critical lesson:** Breakeven SL = **false security**. When index returns to entry (0 pts loss), options have been decaying for the full duration. Trade 806: index said "breakeven", account said -₹1,544. Breakeven SL removed — time exit (13:00) and options trail (30%) replace it.

**Pattern:**
| Market | CE (calls) | PE (puts) |
|--------|-----------|----------|
| Strong uptrend | ✅ | ❌ |
| Choppy/recovery | ❌ IV crush | ❌ |
| Strong downtrend | ❌ | ✅ |
| Reversal after rally | ❌ | ✅ IV expansion |
        """)

    with st.expander("Aug 21, 2026 — Theta decay lesson: held too long. -₹630 options, +33 index pts."):
        st.markdown("""
**1 trade (788). DBR demand CE. Entry 09:16, EOD close 15:20.**

By 10:45 the option was +₹500. Index stalled. Theta bled premium all afternoon.
EOD close: +33 index pts but CE decayed to -₹630.

**Rule:** ATM CE held for 6 hours = theta erodes delta gains. Exit at 13:00 or when +30% options profit — never hold hoping index resumes.

**Fix applied:** `OPTIONS_TRAIL_PCT=30`, `TIME_EXIT_HOUR=13` — both auto-exit without human involvement.
        """)

    with st.expander("Aug 20, 2026 — Don't panic-exit on options price. Watch index level only."):
        st.markdown("""
**3 trades. Trade 770 closed manually at +₹16. Correct hold would have been ~₹3,000.**

- Supply zone: Nifty fell from 24226 to 24039 (-187 pts, 104 pts past target)
- User panicked on options price fluctuation — closed manually at +₹16 instead of target
- Trade 779 (same zone type, held 2+ hrs): +₹3,415

**The rule: If Nifty has NOT crossed your SL level, the trade is still valid — regardless of options price.**

Options premium fluctuates with IV, delta, bid-ask spread. Watching it live causes panic and premature exits. Index level is the only truth during a trade.
        """)

    st.divider()

    # ── Learning log ──────────────────────────────────────────────────────
    st.subheader("Learning Log")
    _log_file = config.BASE_DIR / "logs" / "autolearn.log"
    if _log_file.exists():
        _log_lines = _log_file.read_text(encoding="utf-8", errors="ignore").strip().splitlines()
        if _log_lines:
            with st.expander(f"📋 Last {min(50, len(_log_lines))} log entries", expanded=True):
                st.code("\n".join(_log_lines[-50:]), language=None)
        else:
            st.info("Log file is empty — no learning events yet.")
    else:
        st.info("No learning log yet. It appears after the first 10 closed trades.")


# ══════════════════════════════════════════════════════════════════════════
# TAB 6 — TUTORIAL
# ══════════════════════════════════════════════════════════════════════════
with tab_tutorial:
    st.header("📖 Tutorial & Notes")
    st.caption("Reference guide — accurate as of August 2026.")

    # ── Morning Checklist ─────────────────────────────────────────────────
    with st.expander("☀️ Daily Morning Checklist", expanded=True):
        st.markdown("""
**Do this every trading day before 9:15 AM:**

1. Open dashboard → **Engine tab**
2. Click **"🔑 Login to Kite"** — opens Kite login page in a new tab
3. Login with Kite password + TOTP (6-digit authenticator code)
4. Kite redirects back to the dashboard — token is auto-captured
5. Dashboard shows ✅ **Token valid for today**
6. Click **▶ Start Engine** — sidebar shows 🟢 Running

> **Token expires every day at midnight** (SEBI requirement — cannot be automated).
> If you miss the morning login, the engine runs but live orders will fail silently.
        """)

    # ── Understanding Signals ─────────────────────────────────────────────
    with st.expander("📡 Understanding Signals"):
        st.markdown("""
**Signal = a zone the engine found worth trading.**

You receive a Telegram message with:
- Zone type, timeframe, entry price, stop loss, target
- Booster score and confluence (how many TFs agree)
- ✅ Approve / ❌ Reject buttons
- ⚠️ Expiry day warning if today is Tuesday (next week's contract will be used)

**Zone Types:**
| Type | Full Name | Class | Bias |
|------|-----------|-------|------|
| DBR | Drop-Base-Rally | Demand | Buy CE |
| RBR | Rally-Base-Rally | Demand | Buy CE |
| RBD | Rally-Base-Drop | Supply | Buy PE |
| DBD | Drop-Base-Drop | Supply | Buy PE |

**Confluence:** Number of timeframes (5min, 15min, 60min) agreeing at the same price.
Higher = stronger signal.

**Trend filter:** Demand zones (CE) are only signalled when 60min trend is UP.
Supply zones (PE) only when 60min trend is DOWN. No counter-trend trades.

**Same zone can appear multiple times** if previous attempts were rejected.
Always approve or reject — never leave a signal pending.
        """)

    # ── Live Order Flow ───────────────────────────────────────────────────
    with st.expander("🔴 Live Order Flow — What Happens When You Approve"):
        st.markdown("""
**Exact sequence when you tap ✅ Approve on Telegram:**

1. **Entry validation** — checks if Nifty is still near the signal's entry price
   - Tolerance = 50% of SL distance (max 20 pts)
   - If price moved too far in the wrong direction → signal auto-rejected, try next

2. **Contract selection** — finds ATM options contract
   - Strike = signal's entry price rounded to nearest 50
   - Expiry = next Tuesday (NSE changed Nifty weekly expiry from Thursday → Tuesday)
   - Tuesday itself → uses NEXT week (never same-day expiry)

3. **Limit order placed on Kite**
   - BUY at option LTP + 2 pts (rounded to tick 0.05)
   - Falls back to market order only if option LTP unavailable
   - kite_order_id stored in DB and visible in Signals tab

4. **Trade monitored every 1 minute**
   - Target hit → SELL limit order placed automatically
   - Stop loss hit → SELL limit order placed automatically
   - 15:20 EOD → SELL limit order placed, CSV exported

**If order fails:**
- Signal is auto-rejected (you can approve the next one immediately)
- Telegram sends failure reason
- Failure reason saved in DB notes column

**Important:** After approval, check your **Kite app** to confirm the order appeared.
If kite_order_id is empty in Signals tab → order was never placed.
        """)

    # ── Nifty Options — Key Facts ─────────────────────────────────────────
    with st.expander("📋 Nifty Options — Key Facts"):
        st.markdown("""
**Expiry:** Every Tuesday (NSE changed from Thursday — effective 2026)

**Lot size:** 65 units per lot (NSE revised Jan 2026, reduced from 75 — auto-fetched from Kite)

**Strike intervals:** Every 50 points (e.g., 24300, 24350, 24400...)

**Order type:** Limit at LTP ± 2 pts (not market — avoids wide spread slippage)

**Product type:** MIS (intraday only — positions auto-squared off by Kite at 15:30 if not closed)

**Minimum margin required:** Approximately ₹15,000–25,000 per lot for ATM options
(actual premium varies with market conditions)

**Tuesday expiry day rule:** System always skips same-day expiry and uses next week's contract.
Same-day expiry options have extreme gamma risk — premium can halve in minutes.

**SL/Target tracking:** Tracked in Nifty index points, not option premium.
Actual options P&L may differ due to theta decay and IV changes.
        """)

    # ── Settings Guide ────────────────────────────────────────────────────
    with st.expander("⚙️ Settings — What Each One Does"):
        st.markdown("""
| Setting | Value (Aug 29 2026) | Why |
|---------|-------|-----|
| Entry Timeframe | 5minute | Best signal volume and accuracy |
| Min Booster Score | 10 | Perfect setups only (3y backtest: score is a weak quality filter, but no harm going strict) |
| Min Confluence | 3 | All 3 TFs must agree — this is what actually concentrates quality |
| Zone Approach | 30 pts | Tighter = enter closer to the line (was 50) |
| Min Risk | 15 pts | Skip degenerate tiny-base zones (target would be a few pts away) |
| Signal Expiry | 45 min | Pending signals older than 45 min auto-expired |
| SL Buffer | 5 pts | Extra buffer beyond zone distal — avoids wick stop-outs |
| Zone Classes | **supply only** | 3-year backtest: demand/CE had negative expectancy (−245 pts). PE carries the strategy. |
| Max Trades/Day | 2 | Hard cap. Real frequency at this quality bar ≈ 1 trade/month. |
| Scan Window | 09:15–10:30 | Golden window from live analysis; afternoon options trades consistently lose |

At this quality bar the system will go days or weeks with no signal. That is expected —
it is waiting for a supply zone with full 3-timeframe confluence in the morning window.
        """)

    # ── Time of Day Insights ──────────────────────────────────────────────
    with st.expander("🕐 Time of Day — What the Data Shows"):
        st.markdown("""
**Analysis of paper trades (as of Aug 2026):**

| Hour | Win% | Avg P&L | Note |
|------|------|---------|------|
| 09:xx | 67% | 43.9 pts | Too few trades |
| 10:xx | 75% | 39.5 pts | Solid |
| 11:xx | 78% | 48.2 pts | Strong |
| 12:xx | 50% | 8.8 pts | ⚠️ Weakest — lunch hour chop |
| 14:xx | 83% | 63.5 pts | ⭐ Best hour |

**Action:** Consider skipping 12:00–13:00 in Scan Window once more live data confirms.
        """)

    # ── Troubleshooting ───────────────────────────────────────────────────
    with st.expander("🔧 Troubleshooting"):
        st.markdown("""
**No signals for days:**
- Check logs: `journalctl -u trading --since today | tail -50`
- "demand zone but 60min trend is DOWN" → normal, market is bearish
- "confluence < min" → reduce Min Confluence to 1
- Verify engine running: sidebar shows 🟢

**Order failed / auto-rejected:**
- Telegram shows failure reason
- Signal is automatically rejected — approve the next signal
- Check kite_order_id in Signals tab — empty = no order ever placed

**Contract not found:**
- Most common cause: expiry day calculation wrong
- Check logs: `journalctl -u trading | grep "Expiry dates"`
- Logs show actual expiry dates Kite returns — compare with code

**Token error mid-day:**
- Kite token valid only for today — regenerate if you see "invalid token"
- Engine tab → Login to Kite → save token again → restart engine

**Same zone appearing repeatedly:**
- Normal after an order failure — zone re-signals when previous attempt was rejected
- Reject all old signals, wait for fresh ones after zone is re-touched

**Data loss warning:**
- Never `git reset --hard` on VPS without backup:
  `cp ~/Trading_system/data/trades.db ~/trades_backup_$(date +%Y%m%d).db`
- Or use Telegram backup button in Engine tab → section 6

**Auto-learn disabled a setting:**
- Learning tab → Re-enable section → re-enable one by one
        """)

    # ── Stable Versions ───────────────────────────────────────────────────
    with st.expander("🏷️ Stable Versions — How to Tag and Restore"):
        st.markdown("""
**Tag a stable version after a successful trading day:**

```bash
# Find the commit hash (look for the date you want)
git log --format="%h %ad %s" --date=short

# Create a tag on that commit
git tag -a v1.0-stable <commit-hash> -m "Description of what's working"

# Push tag to GitHub
git push origin v1.0-stable
```

You can also create tags from **GitHub UI:**
Repo → Releases → Draft a new release → Choose a tag → pick the commit.

---

**Check existing tags:**
```bash
git tag -l                  # list all tags
git show v1.0-stable --stat # see what's in a tag
```

---

**Restore to a stable version on VPS (when something breaks):**
```bash
git fetch --tags
git checkout v1.0-stable
sudo systemctl restart trading.service
```

**Return to latest after restoring:**
```bash
git checkout main && git pull
sudo systemctl restart trading.service
```

---

**Current stable tags:**

| Tag | Date | Description |
|-----|------|-------------|
| v1.0-stable | Aug 14, 2026 (commit a09340b) | First successful live trading day — Aug 17, 3/3 target hits |
        """)

    # ── VPS Quick Reference ───────────────────────────────────────────────
    with st.expander("🖥️ VPS Quick Reference"):
        st.markdown("""
**Server:** AWS Lightsail Mumbai | IP: 13.201.210.4 | User: ubuntu

```bash
# Check engine status
sudo systemctl status trading

# Restart engine (always after git pull)
sudo systemctl restart trading

# View live logs
journalctl -u trading -f

# View today's logs
journalctl -u trading --since today

# Search logs for order activity
journalctl -u trading | grep -i "order\\|approve\\|kite\\|error"

# Backup trades DB
cp ~/Trading_system/data/trades.db ~/trades_backup_$(date +%Y%m%d).db

# Pull latest code and restart
cd ~/Trading_system && git pull && sudo systemctl restart trading
```

**Ports open in Lightsail firewall:**
- 8501 — Streamlit dashboard (public)
        """)

    # ── Lessons Learned ───────────────────────────────────────────────────
    with st.expander("📝 Lessons Learned from Real Trades", expanded=False):
        st.markdown("""
**Aug 17, 2026 — First live day (3/3 target hits ✅)**
- Demand zones (CE) work best on strong UP trending days
- Never close manually on Kite — always use Telegram "Early Exit" button or dashboard close
- Manual Kite close while system is open = double-exit risk (system places second SELL → naked short)

**Aug 18, 2026 — Expiry Tuesday (no signals)**
- Expiry days are noisy — zones get violated more often, signal quality drops

**Aug 19, 2026 — First supply zone trade (loss ❌)**
- Supply zones (PE) need a STRONG down-trending day, not a ranging/weak day
- Enabling a new feature (supply zones) for the first time on a live day is risky — observe in paper first
- Reject signals when you're distracted (phone call, multitasking) — a bad trade costs more than a missed one

---

**Rules that hold:**
1. Check 60min trend before approving — if trend is against your zone class, skip
2. Only approve when you can watch the trade for the next 30 minutes
3. One trade at a time — never have two open positions
4. Let the system exit — don't touch Kite manually after approving
        """)

    # ── Roadmap / Future Plans ────────────────────────────────────────────
    with st.expander("🗺️ Roadmap — What's Planned Next"):
        st.markdown("""
### Instruments
- [x] **Nifty 50** — live trading, working ✅
- [ ] **Bank Nifty** — higher volatility, higher premium; add after Nifty is consistently profitable
- [ ] **Nifty Midcap / FinNifty** — evaluate after Bank Nifty

### Strategy Improvements
- [x] Breakeven SL — SL moves to entry at 1:1 R:R ✅ *(Aug 2026)*
- [ ] **Trailing SL** — follow price to lock in more profit
- [ ] **Curve analysis** — check HTF position before entering (Actions Table)
- [ ] **HPA levels** — mark Previous Day High/Low, Current Day High/Low, Big Round Numbers
- [ ] **First Trouble Area (FTA)** — skip trade if opposing zone sits between entry and target
- [ ] **Opening bias filter** — Opening Low = Bullish, Opening High = Bearish

### Risk & Execution
- [ ] **Position verification before SELL** — check Kite positions API to prevent naked short
- [ ] **Options-based SL** — stop out if option premium drops X% regardless of index
- [ ] **Score ≥ 10 for auto-execution** — training says Type 1 entry only at score 10

### Automation
- [ ] **Full agent mode** — remove all human approvals, system decides end-to-end
- [ ] **Auto Kite login** — headless browser to renew token daily without manual step

### Data & Reporting
- [ ] **Live options P&L on dashboard** — real-time premium tracking per open trade
- [ ] **Weekly/monthly summary** — cumulative stats across days
        """)

    st.info("💡 Update this tab whenever you discover something new — it's your permanent reference.")


# ══════════════════════════════════════════════════════════════════════════
# TAB 7 — ZONES  (zone quality inspector)
# ══════════════════════════════════════════════════════════════════════════
with tab_zones:
    st.header("Zone Inspector")
    st.caption(
        "Fetches live Nifty candles from Kite and runs zone detection. "
        "Shows departure strength (leg-out / ATR) and base compression (base range / ATR) "
        "for every detected zone. Requires today's Kite token."
    )

    _z_settings = config.load_settings()
    _z_tfs = _z_settings.get("SCAN_TIMEFRAMES", [config.TF_LOWER, config.TF_INTERMEDIATE, config.TF_HIGHER])
    _z_classes = _z_settings.get("SCAN_ZONE_CLASSES", config.SCAN_ZONE_CLASSES)

    _zc1, _zc2, _zc3 = st.columns(3)
    _z_tf_sel = _zc1.selectbox(
        "Timeframe", options=_z_tfs,
        index=0,
        help="Fetch candles and detect zones for this timeframe.",
    )
    _z_days = _zc2.number_input(
        "History (days)", min_value=5, max_value=90, value=30, step=5,
        help="How many days of candles to fetch. More days = older zones visible.",
    )
    _z_class_filter = _zc3.multiselect(
        "Zone class", options=["demand", "supply"],
        default=_z_classes,
        help="Filter to show only these zone classes.",
    )

    _token_ok_z = False
    if config.TOKEN_FILE.exists():
        try:
            import json as _jz
            _tdz = _jz.loads(config.TOKEN_FILE.read_text())
            _token_ok_z = _tdz.get("date") == date.today().isoformat()
        except Exception:
            pass

    if not _token_ok_z:
        st.warning("No valid Kite token for today — go to Engine tab and login first.")

    if st.button("🔍 Scan Zones Now", type="primary", disabled=not _token_ok_z,
                 help="Fetches candles from Kite and runs zone detection."):
        with st.spinner(f"Fetching {_z_days} days of {_z_tf_sel} candles from Kite..."):
            try:
                from brokers.kite_adapter import KiteAdapter as _KAZ
                from engine.zones import detect_zones as _dz, update_zone_state as _uzs, detect_bos as _dbos

                _kaz = _KAZ()
                _candles = _kaz.get_historical(config.NIFTY_SYMBOL, _z_tf_sel, _z_days)
                if not _candles:
                    st.error("No candles returned — check Kite connection.")
                else:
                    _raw_zones = _dz(_candles, _z_tf_sel)

                    # Update touch counts and validity against all candles after zone formed
                    _zone_rows = []
                    for _z in _raw_zones:
                        # Find formation index
                        _fi = next(
                            (k for k, c in enumerate(_candles) if c.timestamp >= _z.formed_at), None
                        )
                        if _fi is not None:
                            _uzs(_z, _candles[_fi + 1:])

                        # BOS: look at candles before zone formation for structural confirmation
                        _pre = _candles[:(_fi + 1)] if _fi is not None else []
                        _bos = _dbos(_pre, lookback=20) if _pre else None
                        _bos_aligned = (
                            _bos is not None and (
                                (_z.zone_class == "demand" and _bos.direction == "bullish") or
                                (_z.zone_class == "supply" and _bos.direction == "bearish")
                            )
                        )

                        _zone_rows.append({
                            "Class":       _z.zone_class,
                            "Type":        _z.zone_type,
                            "Proximal":    round(_z.proximal, 2),
                            "Distal":      round(_z.distal, 2),
                            "Formed":      _z.formed_at.strftime("%m-%d %H:%M"),
                            "Departure":   _z.departure_strength,
                            "Compression": _z.base_compression,
                            "BOS":         "✅" if _bos_aligned else "—",
                            "Touches":     _z.touch_count,
                            "Valid":       "✅" if _z.is_valid else "❌",
                        })

                    st.session_state["_zone_rows"] = _zone_rows
                    st.session_state["_zone_candle_count"] = len(_candles)
                    st.session_state["_zone_tf"] = _z_tf_sel
                    st.success(
                        f"Found {len(_raw_zones)} zones across {len(_candles)} candles "
                        f"({_z_days} days, {_z_tf_sel})"
                    )
            except Exception as _ze:
                st.error(f"Scan failed: {_ze}")

    # ── Results ────────────────────────────────────────────────────────────
    if "zone_rows" in [k.lstrip("_") for k in st.session_state]:
        _rows = st.session_state.get("_zone_rows", [])
    else:
        _rows = []

    if _rows:
        import pandas as _zpd

        _zdf = _zpd.DataFrame(_rows)

        # Apply class filter
        if _z_class_filter:
            _zdf = _zdf[_zdf["Class"].isin(_z_class_filter)]

        if _zdf.empty:
            st.info("No zones match the selected class filter.")
        else:
            # Summary metrics
            _valid_ct = (_zdf["Valid"] == "✅").sum()
            _bos_ct   = (_zdf["BOS"]   == "✅").sum()
            _avg_dep  = _zdf["Departure"].mean()
            _avg_comp = _zdf["Compression"].mean()
            _tight_ct = (_zdf["Compression"] < 0.5).sum()

            _sm1, _sm2, _sm3, _sm4, _sm5 = st.columns(5)
            _sm1.metric("Total Zones",    len(_zdf))
            _sm2.metric("Valid",          _valid_ct)
            _sm3.metric("BOS Confirmed",  _bos_ct)
            _sm4.metric("Avg Departure",  f"{_avg_dep:.2f}×ATR")
            _sm5.metric("Tight Bases (<0.5)", _tight_ct)

            st.caption(
                "**Departure** = leg-out body / ATR  (higher = stronger explosion from base)  |  "
                "**Compression** = base range / ATR  (lower = tighter coil = higher quality)"
            )

            # Colour rows: green demand, red supply, dim invalid
            def _colour_zone_row(row):
                styles = [""] * len(row)
                idx = row.index.tolist()
                base = "background-color:#d4edda;color:#155724" if row["Class"] == "demand" \
                       else "background-color:#f8d7da;color:#721c24"
                if row["Valid"] == "❌":
                    base = "color:#aaaaaa"
                for i in range(len(styles)):
                    styles[i] = base
                # Highlight strong departure
                if "Departure" in idx and row["Valid"] == "✅":
                    dep_i = idx.index("Departure")
                    if row["Departure"] >= 1.5:
                        styles[dep_i] = "background-color:#fff3cd;color:#856404;font-weight:bold"
                # Highlight tight compression
                if "Compression" in idx and row["Valid"] == "✅":
                    cmp_i = idx.index("Compression")
                    if row["Compression"] < 0.5:
                        styles[cmp_i] = "background-color:#cce5ff;color:#004085;font-weight:bold"
                return styles

            _styled_zdf = _zdf.style.apply(_colour_zone_row, axis=1)
            st.dataframe(_styled_zdf, use_container_width=True, hide_index=True)

            st.caption(
                "🟡 Yellow = departure ≥ 1.5× ATR (strong explosion)  |  "
                "🔵 Blue = compression < 0.5× ATR (very tight base)  |  "
                "Grey = invalid (zone broken by price)"
            )

# ══════════════════════════════════════════════════════════════════════════
# TAB 8 — AGENT  (knowledge manager + memory viewer)
# ══════════════════════════════════════════════════════════════════════════
with tab_agent:
    import json as _json
    from pathlib import Path as _Path

    _AGENT_DIR  = _Path(__file__).parent / "agent"
    _KB_DIR     = _AGENT_DIR / "knowledge"
    _MEM_PATH   = _AGENT_DIR / "memory.json"
    _KB_DIR.mkdir(exist_ok=True)

    st.header("🧠 Agent — Knowledge Manager")
    st.caption("Upload external sources (PDFs, notes, articles) and view agent memory.")

    # ── Memory viewer ──────────────────────────────────────────────────────
    with st.expander("📋 Current Memory (memory.json)", expanded=False):
        if _MEM_PATH.exists():
            _mem = _json.loads(_MEM_PATH.read_text(encoding="utf-8"))
            _mc1, _mc2, _mc3, _mc4 = st.columns(4)
            _mc1.metric("Last Trained",   _mem.get("last_trained") or "never")
            _mc2.metric("Regime",         _mem.get("market_regime", "—"))
            _mc3.metric("Caution Flags",  len(_mem.get("caution_flags", [])))
            _mc4.metric("Mistake Log",    len(_mem.get("mistake_log", [])))

            if _mem.get("caution_flags"):
                st.markdown("**Active Cautions**")
                for _cf in _mem["caution_flags"]:
                    st.warning(_cf)

            if _mem.get("win_patterns"):
                st.markdown("**Win Patterns**")
                for _wp in _mem["win_patterns"]:
                    st.success(_wp)

            if _mem.get("mistake_log"):
                st.markdown("**Mistake Log**")
                for _ml in _mem["mistake_log"]:
                    st.error(_ml)

            with st.expander("Raw JSON"):
                st.json(_mem)
        else:
            st.info("memory.json not found. Run trainer.py first.")

    st.divider()

    # ── Ingest new source ──────────────────────────────────────────────────
    st.subheader("➕ Ingest New Knowledge Source")
    _col_left, _col_right = st.columns([2, 1])

    with _col_left:
        _ingest_label = st.text_input("Label (name for this source)", placeholder="e.g. Sam Seiden Method")
        _ingest_type  = st.radio("Source type", ["Upload file", "Paste URL", "Paste text"], horizontal=True)

        _content      = None
        _source_type  = "text"

        if _ingest_type == "Upload file":
            _up = st.file_uploader(
                "Upload PDF, TXT, or Markdown",
                type=["pdf", "txt", "md"],
                help="Max ~20MB. PDF text is extracted automatically."
            )
            if _up:
                if _up.name.endswith(".pdf"):
                    try:
                        import pdfplumber as _pdfplumber
                        import io as _io
                        with _pdfplumber.open(_io.BytesIO(_up.read())) as _pdf:
                            _content = "\n".join(p.extract_text() or "" for p in _pdf.pages)
                        _source_type = "pdf"
                    except ImportError:
                        try:
                            import pypdf as _pypdf
                            import io as _io
                            _reader  = _pypdf.PdfReader(_io.BytesIO(_up.read()))
                            _content = "\n".join(p.extract_text() or "" for p in _reader.pages)
                            _source_type = "pdf"
                        except ImportError:
                            st.error("PDF support needs pdfplumber: pip install pdfplumber")
                else:
                    _content     = _up.read().decode("utf-8", errors="ignore")
                    _source_type = _up.name.split(".")[-1]

                if _content:
                    st.caption(f"Extracted {len(_content):,} characters")

        elif _ingest_type == "Paste URL":
            _url_input = st.text_input("URL", placeholder="https://...")
            if _url_input:
                _source_type = "url"
                _content     = _url_input   # passed as-is; ingest() fetches it

        else:
            _content     = st.text_area("Paste text / notes here", height=200)
            _source_type = "text"

    with _col_right:
        st.markdown("&nbsp;")
        st.markdown("**Tips**")
        st.caption("• PDFs: course materials, books, research papers")
        st.caption("• URLs: articles, blog posts, market analysis")
        st.caption("• Text: paste your own notes or rules directly")
        st.caption("• After ingesting, click **Re-seed Memory** below")

    _can_ingest = bool(_ingest_label and _content and os.environ.get("ANTHROPIC_API_KEY"))

    if st.button("⚡ Ingest Source", disabled=not _can_ingest, type="primary"):
        with st.spinner("Claude is extracting trading insights..."):
            try:
                import sys as _sys
                _sys.path.insert(0, str(_Path(__file__).parent))
                from agent.ingest import ingest as _ingest, _read_url as _fetch_url

                # For URL type, fetch content first
                if _ingest_type == "Paste URL":
                    _content, _total_chars = _fetch_url(_content)
                else:
                    _total_chars = len(_content)

                _ingest(_ingest_label, _content, _source_type, _total_chars)
                st.success(f"✅ '{_ingest_label}' ingested successfully!")
                st.rerun()
            except Exception as _e:
                st.error(f"Ingest failed: {_e}")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        st.warning("ANTHROPIC_API_KEY not set — ingest disabled.")

    st.divider()

    # ── Knowledge library ──────────────────────────────────────────────────
    st.subheader("📚 Knowledge Library")

    _kb_files = sorted(_KB_DIR.glob("*.json"))
    if not _kb_files:
        st.info("No sources ingested yet. Upload your first source above.")
    else:
        for _kf in _kb_files:
            try:
                _kd = _json.loads(_kf.read_text(encoding="utf-8"))
            except Exception:
                continue

            with st.expander(f"**{_kd.get('label', _kf.stem)}** — {_kd.get('ingested_at','?')} | {_kd.get('evidence_type','?')} | conf: {_kd.get('confidence_level','?')}"):
                st.caption(f"Market: {_kd.get('applicable_market','?')} | Timeframe: {_kd.get('applicable_timeframe','?')} | Status: {'🔵 HYPOTHESIS' if _kd.get('is_hypothesis') else '✅ Validated'}")
                st.markdown(f"*{_kd.get('summary', '')}*")

                _k1, _k2, _k3, _k4 = st.columns(4)
                _k1.metric("Key Concepts",   len(_kd.get("key_concepts", [])))
                _k2.metric("Entry Rules",    len(_kd.get("entry_rules", [])))
                _k3.metric("Zone Filters",   len(_kd.get("zone_quality_filters", [])))
                _k4.metric("Limitations",    len(_kd.get("limitations", [])))

                if _kd.get("limitations"):
                    st.markdown("**Limitations / Caveats**")
                    for _lim in _kd["limitations"]:
                        st.caption(f"⚠️ {_lim}")

                if _kd.get("key_concepts"):
                    st.markdown("**Key Concepts**")
                    for _kc in _kd["key_concepts"]:
                        st.markdown(f"• {_kc}")

                if _kd.get("entry_rules"):
                    st.markdown("**Entry Rules (hypothesis)**")
                    for _er in _kd["entry_rules"]:
                        st.markdown(f"• {_er}")

                if _kd.get("cautions"):
                    st.markdown("**Cautions**")
                    for _ca in _kd["cautions"]:
                        st.warning(_ca)

                if st.button(f"🗑️ Remove '{_kd.get('label', _kf.stem)}'", key=f"del_{_kf.stem}"):
                    _kf.unlink()
                    st.success("Removed.")
                    st.rerun()

    st.divider()

    # ── Agent vs Baseline comparison ───────────────────────────────────────
    st.subheader("📊 Agent vs Baseline Performance")
    st.caption(
        "Compares outcomes by agent verdict. SKIP signals show what would have happened "
        "if taken — a good agent should have SKIP WR < overall WR."
    )

    try:
        import sqlite3 as _sq3
        _aconn = _sq3.connect(str(_Path(__file__).parent / "data" / "trades.db"))
        _aconn.row_factory = _sq3.Row
        _rows = _aconn.execute("""
            SELECT
                COALESCE(agent_verdict, 'pre-agent') AS verdict,
                SUM(1) AS total,
                SUM(CASE WHEN COALESCE(result, sim_outcome) IN ('win','target')    THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN COALESCE(result, sim_outcome) IN ('loss','stoploss') THEN 1 ELSE 0 END) AS losses,
                SUM(CASE WHEN COALESCE(result, sim_outcome) NOT IN ('win','target','loss','stoploss') THEN 1 ELSE 0 END) AS neutral,
                ROUND(AVG(COALESCE(pnl_points, sim_pnl_points)), 2) AS avg_pnl,
                SUM(CASE WHEN result IS NOT NULL THEN 1 ELSE 0 END) AS actual_ct,
                SUM(CASE WHEN result IS NULL AND sim_outcome IS NOT NULL THEN 1 ELSE 0 END) AS sim_ct
            FROM signals
            WHERE result IS NOT NULL OR sim_outcome IS NOT NULL
            GROUP BY COALESCE(agent_verdict, 'pre-agent')
            ORDER BY total DESC
        """).fetchall()
        _aconn.close()

        if not _rows:
            st.info("No signal data yet.")
        else:
            _verdict_data = [dict(r) for r in _rows]
            # Overall baseline (all decided signals)
            _all_wins   = sum(r["wins"]   for r in _verdict_data)
            _all_losses = sum(r["losses"] for r in _verdict_data)
            _baseline_wr = round(_all_wins / (_all_wins + _all_losses) * 100, 1) if (_all_wins + _all_losses) else 0

            _vcols = st.columns(len(_verdict_data) + 1)
            _vcols[0].metric("Baseline WR", f"{_baseline_wr}%", help="All decided signals, no AI filter")

            _verdict_labels = {
                "TRADE":      "✅ TRADE",
                "SKIP":       "🚫 SKIP",
                "REVIEW":     "⚠️ REVIEW",
                "pre-agent":  "📅 Pre-agent",
            }
            for _i, _vd in enumerate(_verdict_data):
                _v   = _vd["verdict"]
                _dec = _vd["wins"] + _vd["losses"]
                _wr  = round(_vd["wins"] / _dec * 100, 1) if _dec else 0
                _delta = round(_wr - _baseline_wr, 1) if _v != "pre-agent" else None
                _vcols[_i + 1].metric(
                    _verdict_labels.get(_v, _v),
                    f"{_wr}% WR",
                    delta=f"{_delta:+.1f}% vs baseline" if _delta is not None else None,
                    delta_color="normal" if _v == "TRADE" else ("inverse" if _v == "SKIP" else "off"),
                    help=f"{_vd['total']} signals | {_vd['wins']}W / {_vd['losses']}L / {_vd['neutral']}N | avg {_vd['avg_pnl']:+.1f}pts"
                )

            # Detailed table
            import pandas as _pd_agent
            _df_agent = _pd_agent.DataFrame([
                {
                    "Verdict":      _verdict_labels.get(r["verdict"], r["verdict"]),
                    "Total":        r["total"],
                    "Actual":       r["actual_ct"],
                    "Simulated":    r["sim_ct"],
                    "Wins":         r["wins"],
                    "Losses":       r["losses"],
                    "Neutral":      r["neutral"],
                    "WR %":         f"{round(r['wins']/(r['wins']+r['losses'])*100,1) if (r['wins']+r['losses']) else 0}%",
                    "Avg PnL pts":  r["avg_pnl"],
                }
                for r in _verdict_data
            ])
            st.dataframe(_df_agent, use_container_width=True, hide_index=True)

            # Interpretation
            _skip_rows = [r for r in _verdict_data if r["verdict"] == "SKIP"]
            if _skip_rows:
                _sr = _skip_rows[0]
                _skip_dec = _sr["wins"] + _sr["losses"]
                _skip_wr  = round(_sr["wins"] / _skip_dec * 100, 1) if _skip_dec else 0
                if _skip_wr < _baseline_wr - 5:
                    st.success(f"Agent is adding value: SKIP signals had {_skip_wr}% WR vs {_baseline_wr}% baseline — correctly avoiding poor setups.")
                elif _skip_wr > _baseline_wr + 5:
                    st.error(f"Agent may be counterproductive: SKIP signals had {_skip_wr}% WR vs {_baseline_wr}% baseline — skipping winners.")
                else:
                    st.info(f"Agent effect is neutral so far: SKIP WR={_skip_wr}% vs baseline {_baseline_wr}%. More data needed.")
            else:
                st.info("No SKIP verdicts recorded yet. Data will populate once agent has trained and is evaluating signals.")
    except Exception as _ae:
        st.warning(f"Could not load agent performance data: {_ae}")

    st.divider()

    # ── Hypothesis tracker ─────────────────────────────────────────────────
    st.subheader("🔬 Hypothesis Tracker")
    st.caption("External rules are tested against live trade data. 20+ signals required to validate or reject.")

    _ht = {}
    if _MEM_PATH.exists():
        try:
            _ht = _json.loads(_MEM_PATH.read_text(encoding="utf-8")).get("hypothesis_tracker", {})
        except Exception:
            pass

    if not _ht:
        st.info("No hypotheses tracked yet. Ingest a source and run the trainer to populate.")
    else:
        _status_emoji = {"validated": "✅", "rejected": "❌", "testing": "🔄", "inconclusive": "⚠️", "untested": "◇"}
        for _src_slug, _src in _ht.items():
            rules = _src.get("rules", [])
            if not rules:
                continue
            with st.expander(f"**{_src.get('source', _src_slug)}** — {len(rules)} testable rules"):
                for _r in rules:
                    _st  = _r.get("status", "untested")
                    _tot = _r.get("signals_tested", 0)
                    _w   = _r.get("wins", 0)
                    _l   = _r.get("losses", 0)
                    _wr  = round(_w / (_w + _l) * 100) if (_w + _l) else 0
                    _em  = _status_emoji.get(_st, "◇")
                    st.markdown(
                        f"{_em} **{_st.upper()}** — {_r['rule']}  \n"
                        f"  `{_tot} signals tested | {_w}W / {_l}L | {_wr}% WR`"
                    )

    st.divider()

    # ── Candidate memory promote ───────────────────────────────────────────
    st.subheader("📋 Candidate Memory Files")
    st.caption("Trainer and seeder write candidates — review before promoting to live memory.json.")

    _candidates = sorted(_AGENT_DIR.glob("memory_candidate_*.json"), reverse=True)
    if not _candidates:
        st.info("No candidate files yet. Candidates appear after trainer.py or seed_memory.py runs.")
    else:
        for _cand in _candidates:
            try:
                _cd = _json.loads(_cand.read_text(encoding="utf-8"))
            except Exception:
                continue
            with st.expander(f"**{_cand.name}** — trained: {_cd.get('last_trained','?')}, regime: {_cd.get('market_regime','?')}"):
                _cc1, _cc2, _cc3 = st.columns(3)
                _cc1.metric("Mistakes",   len(_cd.get("mistake_log", [])))
                _cc2.metric("Wins",       len(_cd.get("win_patterns", [])))
                _cc3.metric("Cautions",   len(_cd.get("caution_flags", [])))
                with st.expander("Raw candidate JSON"):
                    st.json(_cd)
                _col_promote, _col_discard = st.columns(2)
                if _col_promote.button(f"✅ Promote to Live", key=f"promote_{_cand.stem}", type="primary"):
                    try:
                        import subprocess as _sp
                        import sys as _sys
                        _pr = _sp.run(
                            [_sys.executable, str(_AGENT_DIR / "promote_memory.py"),
                             "--date", _cand.stem.replace("memory_candidate_", ""), "--force"],
                            capture_output=True, text=True,
                            cwd=str(_Path(__file__).parent),
                        )
                        if _pr.returncode == 0:
                            st.success("✅ Promoted to memory.json!")
                        else:
                            st.error(_pr.stderr[-500:])
                        st.rerun()
                    except Exception as _e:
                        st.error(f"Promote failed: {_e}")
                if _col_discard.button(f"🗑️ Discard Candidate", key=f"discard_{_cand.stem}"):
                    _cand.unlink()
                    st.success("Discarded.")
                    st.rerun()

    st.divider()

    # ── Re-seed memory ─────────────────────────────────────────────────────
    st.subheader("🔄 Re-seed Memory from All Sources")
    st.caption("Combines all trades + AGENT_KNOWLEDGE.md + ingested sources → creates a CANDIDATE file (does NOT overwrite live memory.json). Review above, then promote.")

    _can_seed = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if st.button("🔄 Create New Candidate Memory", disabled=not _can_seed, type="secondary"):
        with st.spinner("Claude Sonnet synthesising memory from all sources... (30–60s)"):
            try:
                import subprocess as _sp
                import sys as _sys
                _result = _sp.run(
                    [_sys.executable, str(_AGENT_DIR / "seed_memory.py")],
                    capture_output=True, text=True, input="y\n",
                    cwd=str(_Path(__file__).parent),
                )
                if _result.returncode == 0:
                    st.success("✅ Candidate created — review above and promote when ready.")
                    st.code(_result.stdout[-1500:] if len(_result.stdout) > 1500 else _result.stdout)
                else:
                    st.error("Seeder failed")
                    st.code(_result.stderr[-1000:])
                st.rerun()
            except Exception as _e:
                st.error(f"Failed: {_e}")
