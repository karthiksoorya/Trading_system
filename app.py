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
    approve_signal, reject_signal, reject_all_pending,
)
from journal.export import export_day
from scheduler import is_market_open, get_last_trading_day

# ── Page setup ────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Nifty Trading System",
    page_icon="📈",
    layout="wide",
)
init_db()

# ── Engine helpers ────────────────────────────────────────────────────────

def _engine_pid() -> int | None:
    try:
        return int(config.ENGINE_PID_FILE.read_text().strip())
    except Exception:
        return None

def is_engine_running() -> bool:
    pid = _engine_pid()
    if pid is None:
        return False
    try:
        if sys.platform == "win32":
            # os.kill(pid, 0) is unreliable on Windows — use tasklist instead
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
    proc = subprocess.Popen(
        [sys.executable, str(config.BASE_DIR / "main.py"), "--run"],
        cwd=str(config.BASE_DIR),
    )
    config.ENGINE_PID_FILE.write_text(str(proc.pid))

def stop_engine():
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
    st.caption(f"Mode: **{config.MODE.upper()}**")
    st.divider()

    engine_running = st.session_state.get("engine_on", is_engine_running())
    st.metric("Engine",       "🟢 Running" if engine_running else "🔴 Stopped")
    _pending = pending_count()
    st.metric("Pending",      f"🔔 {_pending} signal(s)" if _pending else "✅ None")
    st.metric("Trades Today", trades_today())
    st.metric("Daily P&L",    f"{daily_pnl():.2f} pts")
    st.divider()

    if st.button("🔄 Refresh page"):
        st.rerun()

    st.caption(f"Updated: {datetime.now().strftime('%H:%M:%S')}")

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
tab_engine, tab_approvals, tab_signals, tab_performance = st.tabs([
    "🔧 Engine", _pending_label, "📊 Signals", "📈 Performance"
])

# ══════════════════════════════════════════════════════════════════════════
# TAB 1 — ENGINE CONTROL
# ══════════════════════════════════════════════════════════════════════════
with tab_engine:
    st.header("Engine Control")

    # ── Token ─────────────────────────────────────────────────────────────
    st.subheader("1. Generate Today's Token")
    st.caption("Required once every morning before the engine can run.")

    try:
        from brokers.kite_adapter import KiteAdapter
        k = KiteAdapter()
        login_url = k.generate_login_url()
        st.markdown(f"**Step 1 →** [Click here to log in to Kite]({login_url})")
    except Exception as e:
        st.error(f"Could not generate login URL: {e}")
        login_url = None

    raw_url = st.text_input(
        "Step 2 → Paste the full redirect URL from your browser address bar",
        placeholder="http://127.0.0.1/?request_token=XXXXXX&status=success",
    )
    if st.button("💾 Save Token", disabled=not raw_url):
        token = _extract_token(raw_url)
        if not token:
            st.error("Could not find request_token in the URL. Paste the full redirect URL.")
        else:
            try:
                k.generate_session(token)
                st.success("✅ Token saved. Engine is ready to start.")
            except Exception as e:
                st.error(f"Token exchange failed: {e}")

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

    # ── Settings ──────────────────────────────────────────────────────────
    st.subheader("4. Settings")
    st.caption("Changes take effect on the next engine start.")

    _current = config.load_settings()

    sl_buffer = st.slider(
        "Stop Loss Buffer (points beyond distal line)",
        min_value=0, max_value=30,
        value=_current.get("SL_BUFFER_POINTS", config.SL_BUFFER_POINTS),
        step=1,
        help="0 = SL exactly at zone edge (pure price action). "
             "5–10 = buffer to avoid being stopped by wicks.",
    )

    if st.button("💾 Save Settings"):
        config.save_settings({"SL_BUFFER_POINTS": sl_buffer})
        st.success(f"Saved — SL buffer: {sl_buffer} pts. Restart engine to apply.")

# ══════════════════════════════════════════════════════════════════════════
# TAB 2 — APPROVALS
# ══════════════════════════════════════════════════════════════════════════
with tab_approvals:
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
            n = reject_all_pending()
            st.toast(f"Rejected {n} signals — data kept for analysis.", icon="🗑")
            st.rerun()
        st.divider()

    if not pending_rows:
        st.success("No pending signals — all caught up.")
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
                m1.metric("Entry",    f"{r['entry']:.2f}")
                m2.metric("Stop Loss", f"{r['stop_loss']:.2f}")
                m3.metric("Target",   f"{r['intraday_target']:.2f}")
                m4.metric("Score",    f"{r['booster_score']:.1f}/10")
                m5.metric("Type",     f"Type {r['entry_type']}")

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
                    st.toast(f"Signal #{r['id']} approved — trade is active.", icon="✅")
                    st.rerun()
                if br.button("❌ Reject", use_container_width=True, key=f"rej_{r['id']}"):
                    reject_signal(r["id"])
                    st.toast(f"Signal #{r['id']} rejected.", icon="❌")
                    st.rerun()

# ══════════════════════════════════════════════════════════════════════════
# TAB 3 — SIGNALS
# ══════════════════════════════════════════════════════════════════════════
with tab_signals:
    st.header("Signals")
    selected_date = st.date_input("Date", value=date.today())
    rows = get_signals_for_date(selected_date.isoformat())

    if not rows:
        st.info("No signals logged for this date.")
    else:
        df = pd.DataFrame([dict(r) for r in rows])

        display_cols = [
            "id", "status", "date", "time_signal", "zone_type", "zone_class", "timeframe",
            "entry", "stop_loss", "intraday_target",
            "booster_score", "confluence_count", "confluence_tfs",
            "entry_type", "position_size",
            "exit_price", "exit_reason", "pnl_points", "result",
        ]
        display_cols = [c for c in display_cols if c in df.columns]

        def _colour(val):
            if val == "win":  return "background-color:#d4edda;color:#155724"
            if val == "loss": return "background-color:#f8d7da;color:#721c24"
            return ""

        styled = df[display_cols].style
        if "result" in display_cols:
            styled = styled.map(_colour, subset=["result"])
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
                        close_trade(trade_id, exit_price, exit_reason, notes)
                        st.success(f"Trade #{trade_id} closed at {exit_price}.")
                        st.rerun()
        else:
            st.success("No open trades for this date.")

        st.divider()

        # ── Export ────────────────────────────────────────────────────────
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
# TAB 3 — PERFORMANCE
# ══════════════════════════════════════════════════════════════════════════
with tab_performance:
    st.header("Performance")

    try:
        con = sqlite3.connect(config.DB_PATH)
        all_df = pd.read_sql("SELECT * FROM signals WHERE result IS NOT NULL", con)
        con.close()

        if all_df.empty:
            st.info("No closed trades yet. Start paper trading to see stats here.")
        else:
            total    = len(all_df)
            wins     = (all_df["result"] == "win").sum()
            losses   = (all_df["result"] == "loss").sum()
            win_rate = wins / total * 100 if total else 0
            total_pnl = all_df["pnl_points"].sum()

            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Total Trades", total)
            m2.metric("Win Rate",     f"{win_rate:.1f}%")
            m3.metric("Wins",         int(wins))
            m4.metric("Losses",       int(losses))
            m5.metric("Total P&L",    f"{total_pnl:.2f} pts")

            st.divider()

            # Validation checklist from master doc
            st.subheader("Validation Checklist (before going live)")
            avg_rr = all_df["pnl_points"].mean() if total else 0

            st.checkbox(f"20+ trades logged ({total} so far)",    value=total >= 20)
            st.checkbox(f"Win rate > 50% ({win_rate:.1f}%)",      value=win_rate > 50)
            st.checkbox(f"Avg P&L positive ({avg_rr:.2f} pts)",   value=avg_rr > 0)
            st.checkbox("System detects zones correctly",          value=False)
            st.checkbox("No crashes for 5 consecutive days",      value=False)

            st.divider()
            st.subheader("All Closed Trades")
            st.dataframe(all_df, use_container_width=True, hide_index=True)

    except Exception as e:
        st.warning(f"Could not load performance data: {e}")
