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
                    target: float, score: float, confluence: str):
    from datetime import datetime
    emoji     = "🟢" if zone_class == "demand" else "🔴"
    direction = "LONG" if zone_class == "demand" else "SHORT"
    now       = datetime.now().strftime("%H:%M:%S")
    text = (
        f"{emoji} <b>Signal #{signal_id} — {direction}</b>  🕐 {now}\n"
        f"{zone_type} | {timeframe}\n"
        f"Entry: {entry:.2f} | SL: {sl:.2f} | TGT: {target:.2f}\n"
        f"Score: {score:.1f}/10 | {confluence}"
    )
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"approve_{signal_id}"},
            {"text": "❌ Reject",  "callback_data": f"reject_{signal_id}"},
        ]]
    }
    _send(text, reply_markup=keyboard)


def trade_approved(signal_id: int, entry: float, sl: float, target: float):
    _send(
        f"✅ <b>Trade #{signal_id} Approved</b>\n"
        f"Entry: {entry:.2f} | SL: {sl:.2f} | Target: {target:.2f}\n"
        f"Monitoring for auto-exit..."
    )


def trade_closed(signal_id: int, exit_price: float, reason: str, pnl: float):
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
    _send(
        f"{emoji} <b>Trade #{signal_id} Closed — {label}</b>\n"
        f"Exit: {exit_price:.2f} | P&L: {pnl_str} pts"
    )


def autolearn_alert(message: str):
    _send(f"🤖 <b>Auto-Learn</b>\n{message}\n\nRe-enable via Performance tab if market conditions change.")


def backup_result(success: bool, message: str):
    if success:
        _send(f"☁️ <b>Backup OK</b>\n{message}")
    else:
        _send(f"❌ <b>Backup Failed</b>\n{message}")


def eod_summary(trades: int, wins: int, losses: int, total_pnl: float):
    pnl_str = f"+{total_pnl:.2f}" if total_pnl >= 0 else f"{total_pnl:.2f}"
    emoji = "📈" if total_pnl >= 0 else "📉"
    _send(
        f"{emoji} <b>EOD Summary</b>\n"
        f"Trades: {trades} | Wins: {wins} | Losses: {losses}\n"
        f"Net P&L: {pnl_str} pts"
    )
