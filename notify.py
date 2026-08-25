"""
Telegram notifications for the trading system.
Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from .env via config.
"""

import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
_URL     = f"https://api.telegram.org/bot{_TOKEN}/sendMessage"


def _send(text: str, reply_markup: dict | None = None) -> int | None:
    if not _TOKEN or not _CHAT_ID:
        logger.warning("Telegram not configured — skipping notification.")
        return None
    payload = {"chat_id": _CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(_URL, json=payload, timeout=5)
        result = r.json()
        if result.get("ok"):
            return result["result"]["message_id"]
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)
    return None


def signal_detected(signal_id: int, zone_class: str, zone_type: str,
                    timeframe: str, entry: float, sl: float,
                    target: float, score: float, confluence: str,
                    strike: int = 0, opt_type: str = "",
                    delta: float = 0.0, vix: float = 0.0,
                    iv_rank: float | None = None):
    from datetime import datetime, date, timedelta
    emoji     = "🟢" if zone_class == "demand" else "🔴"
    direction = "LONG" if zone_class == "demand" else "SHORT"
    now       = datetime.now().strftime("%H:%M:%S")

    # Expiry day warning (Tuesday = weekly expiry day)
    today = date.today()
    expiry_note = ""
    if today.weekday() == 1:   # Tuesday
        next_expiry = today + timedelta(days=7)
        expiry_note = f"\n⚠️ <b>Expiry day</b> — order will use next week ({next_expiry.strftime('%d %b')}) contract"

    # Options context line — delta, VIX, and IV Rank help judge premium cost before approving
    options_note = ""
    if strike and opt_type:
        iv_warn = " ⚠️ High IV" if vix and vix > 15 else ""
        vix_str = f"{vix:.1f}" if vix else "—"
        if iv_rank is not None:
            if iv_rank <= 30:
                rank_icon = "🟢"    # cheap — good to buy
            elif iv_rank <= 60:
                rank_icon = "🟡"    # moderate
            else:
                rank_icon = "🔴"    # expensive — IV crush risk
            rank_str = f" | IV Rank: {rank_icon} {iv_rank:.0f}%"
        else:
            rank_str = ""
        options_note = (
            f"\nStrike: <b>{strike} {opt_type}</b> | "
            f"Delta: {delta:+.2f} | VIX: {vix_str}{iv_warn}{rank_str}"
        )

    text = (
        f"{emoji} <b>Signal #{signal_id} — {direction}</b>  🕐 {now}\n"
        f"{zone_type} | {timeframe}\n"
        f"Entry: {entry:.2f} | SL: {sl:.2f} | TGT: {target:.2f}\n"
        f"Score: {score:.1f}/10 | {confluence}"
        f"{options_note}"
        f"{expiry_note}"
    )
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"approve_{signal_id}"},
            {"text": "❌ Reject",  "callback_data": f"reject_{signal_id}"},
        ]]
    }
    _send(text, reply_markup=keyboard)


def signal_auto_approved(signal_id: int, zone_class: str, entry: float, sl: float,
                         target: float, symbol: str, order_id: str):
    """Telegram notification when first trade is auto-executed by the system."""
    emoji = "🟢" if zone_class == "demand" else "🔴"
    direction = "LONG" if zone_class == "demand" else "SHORT"
    keyboard = {
        "inline_keyboard": [[
            {"text": "🚨 Early Exit (system closes on Kite)", "callback_data": f"close_{signal_id}"},
        ]]
    }
    _send(
        f"🤖 <b>Auto-Trade #{signal_id} — First Trade of Day</b>\n"
        f"{emoji} {direction} | {symbol}\n"
        f"Entry: {entry:.2f} | SL: {sl:.2f} | Target: {target:.2f}\n"
        f"Order #{order_id} placed on Kite automatically.\n"
        f"⚙️ System monitors for auto-exit on target / SL / EOD.\n"
        f"⚠️ <b>Do NOT close manually on Kite</b> — use button below only for early exit.",
        reply_markup=keyboard,
    )


def trade_approved(signal_id: int, entry: float, sl: float, target: float):
    keyboard = {
        "inline_keyboard": [[
            {"text": "🚨 Early Exit (system closes on Kite)", "callback_data": f"close_{signal_id}"},
        ]]
    }
    _send(
        f"✅ <b>Trade #{signal_id} Approved — order placed on Kite</b>\n"
        f"Entry: {entry:.2f} | SL: {sl:.2f} | Target: {target:.2f}\n"
        f"⚙️ System will auto-exit on target / SL / EOD.\n"
        f"⚠️ <b>Do NOT close manually on Kite</b> — use button below only if you want an early exit.",
        reply_markup=keyboard,
    )


def breakeven_applied(signal_id: int, entry: float):
    _send(
        f"🔒 <b>Trade #{signal_id} — Breakeven SL Applied</b>\n"
        f"Price reached 1:1 R:R — SL moved to entry {entry:.2f}.\n"
        f"Worst case is now breakeven (zero loss)."
    )


def options_trail_exit(signal_id: int, entry_premium: float, current_premium: float,
                       gain_pct: float, options_pnl: float):
    """Fired when options premium has gained >= OPTIONS_TRAIL_PCT from entry."""
    _send(
        f"📈 <b>Trade #{signal_id} — Options Profit Lock</b>\n"
        f"Premium: ₹{entry_premium:.2f} → ₹{current_premium:.2f} (+{gain_pct:.1f}%)\n"
        f"Options P&L: ₹{options_pnl:+.0f}\n"
        f"Exiting now to protect gains before theta erodes premium."
    )


def time_exit(signal_id: int, hour: int, options_pnl: float | None):
    """Fired when TIME_EXIT_HOUR is reached and trade is still open."""
    pnl_str = f" | Options P&L: ₹{options_pnl:+.0f}" if options_pnl is not None else ""
    _send(
        f"⏰ <b>Trade #{signal_id} — Time Exit ({hour:02d}:00)</b>\n"
        f"Index target not reached — closing to avoid afternoon theta decay.{pnl_str}"
    )


def trade_closed(signal_id: int, exit_price: float, reason: str, pnl: float,
                 options_pnl: float | None = None):
    if reason == "target":
        emoji = "🎯"
        label = "TARGET HIT"
    elif reason == "stoploss":
        emoji = "🛑"
        label = "STOPLOSS HIT"
    elif reason == "eod":
        emoji = "🕒"
        label = "EOD CLOSE"
    else:
        emoji = "📌"
        label = "MANUAL CLOSE"

    pnl_str = f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"
    opts_str = ""
    if options_pnl is not None:
        opts_str = f"\nOptions P&L: ₹{options_pnl:+.0f}"
    _send(
        f"{emoji} <b>Trade #{signal_id} Closed — {label}</b>\n"
        f"Exit: {exit_price:.2f} | Index P&L: {pnl_str} pts{opts_str}"
    )


def autolearn_alert(message: str):
    _send(f"🤖 <b>Auto-Learn</b>\n{message}\n\nRe-enable via Performance tab if market conditions change.")


def backup_result(success: bool, message: str):
    if success:
        _send(f"☁️ <b>Backup OK</b>\n{message}")
    else:
        _send(f"❌ <b>Backup Failed</b>\n{message}")


def eod_summary(trades: int, wins: int, losses: int, total_pnl: float,
                total_options_pnl: float | None = None):
    pnl_str = f"+{total_pnl:.2f}" if total_pnl >= 0 else f"{total_pnl:.2f}"
    emoji = "📈" if total_pnl >= 0 else "📉"
    opts_line = ""
    if total_options_pnl is not None:
        opts_emoji = "✅" if total_options_pnl >= 0 else "❌"
        opts_line = f"\n{opts_emoji} Options P&L: ₹{total_options_pnl:+.0f}"
    _send(
        f"{emoji} <b>EOD Summary</b>\n"
        f"Trades: {trades} | Wins: {wins} | Losses: {losses}\n"
        f"Net Index P&L: {pnl_str} pts{opts_line}"
    )
