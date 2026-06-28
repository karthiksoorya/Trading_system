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
tab_approvals, tab_engine, tab_signals, tab_performance = st.tabs([
    _pending_label, "🔧 Engine", "📊 Signals", "📈 Performance"
])

# Auto-refresh every 30 s — keeps P&L and pending count current without manual click
st.markdown(
    '<meta http-equiv="refresh" content="30">',
    unsafe_allow_html=True,
)

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
        default=_current.get("SCAN_ZONE_CLASSES", ["demand", "supply"]),
        help="Uncheck 'supply' to only trade demand zones (long bias), or vice versa.",
    )

    expiry_minutes = st.slider(
        "Signal Expiry Window (minutes)",
        min_value=15, max_value=120,
        value=_current.get("SIGNAL_EXPIRY_MINUTES", config.SIGNAL_EXPIRY_MINUTES),
        step=5,
        help="Pending signals older than this are auto-expired. Default 45 min.",
    )

    zone_approach = st.slider(
        "Zone Approach Distance (points)",
        min_value=10, max_value=200,
        value=_current.get("ZONE_APPROACH_POINTS", config.ZONE_APPROACH_POINTS),
        step=5,
        help="Signal only fires if LTP is within this many points of the zone proximal. "
             "50 = only near zones. Increase if you miss too many signals.",
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

    if st.button("💾 Save Settings"):
        if not scan_tfs:
            st.error("Select at least one timeframe.")
        elif not scan_classes:
            st.error("Select at least one zone class.")
        else:
            config.save_settings({
                "SL_BUFFER_POINTS":      sl_buffer,
                "ENTRY_TIMEFRAME":       entry_tf,
                "SCAN_TIMEFRAMES":       scan_tfs,
                "SCAN_ZONE_CLASSES":     scan_classes,
                "SIGNAL_EXPIRY_MINUTES": expiry_minutes,
                "ZONE_APPROACH_POINTS":  zone_approach,
                "MIN_BOOSTER_SCORE":     min_score,
                "MIN_CONFLUENCE":        min_conf,
            })
            st.success(
                f"Saved — TF: {entry_tf} | Score ≥ {min_score} | "
                f"Confluence ≥ {min_conf} TF | Approach ≤ {zone_approach} pts | "
                f"Expiry: {expiry_minutes} min"
            )

    st.divider()

    # ── Backup ────────────────────────────────────────────────────────────
    st.subheader("5. Backup")
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
        today_csv = config.CSV_DIR / f"{date.today().isoformat()}.csv"
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
# TAB 2 — APPROVALS
# ══════════════════════════════════════════════════════════════════════════
with tab_approvals:

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
                unreal = (ltp - entry) if zone_class == "demand" else (entry - ltp)
                to_target = abs(target - ltp)
                to_sl     = abs(ltp - sl)
                pnl_color = "🟢" if unreal >= 0 else "🔴"
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

        if ltp is None:
            st.warning("Could not fetch live LTP — token may be expired. P&L shown as — ")
        st.caption("P&L updates on each page refresh. Click **🔄 Refresh page** in the sidebar.")
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
        st.success("No pending signals — all caught up.")
    elif open_trades:
        # Block approvals while a trade is already active
        st.warning(
            f"⚠️ Trade #{open_trades[0]['id']} is still active. "
            "Wait for it to close (target / SL) before approving a new one."
        )
        st.info(f"{len(pending_rows)} signal(s) are queued and will be available once the active trade closes.")
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
                    import notify
                    notify.trade_approved(r["id"], r["entry"], r["stop_loss"], r["intraday_target"])
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
# TAB 4 — PERFORMANCE
# ══════════════════════════════════════════════════════════════════════════
with tab_performance:
    st.header("Performance")

    try:
        con = sqlite3.connect(config.DB_PATH)
        all_df = pd.read_sql("SELECT * FROM signals WHERE result IS NOT NULL", con)
        con.close()

        if all_df.empty:
            st.info("No closed trades yet. Approve and complete a trade to see stats here.")
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

            # ── Validation checklist ──────────────────────────────────────
            st.subheader("Validation Checklist (before going live)")
            avg_pnl = all_df["pnl_points"].mean()
            st.checkbox(f"20+ trades logged ({total} so far)",   value=total >= 20)
            st.checkbox(f"Win rate > 50% ({win_rate:.1f}%)",     value=win_rate > 50)
            st.checkbox(f"Avg P&L positive ({avg_pnl:.2f} pts)", value=avg_pnl > 0)
            st.checkbox("System detects zones correctly",         value=False)
            st.checkbox("No crashes for 5 consecutive days",     value=False)

            st.divider()

            # ── Raw trade log ─────────────────────────────────────────────
            with st.expander("📋 All Closed Trades"):
                st.dataframe(all_df, use_container_width=True, hide_index=True)

    except Exception as e:
        st.warning(f"Could not load performance data: {e}")
