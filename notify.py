"""
Telegram notifications for the trading system.
Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from .env via config.
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)

_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
_URL     = f"https://api.telegram.org/bot{_TOKEN}/sendMessage"


def _send(text: str):
    if not _TOKEN or not _CHAT_ID:
        logger.warning("Telegram not configured — skipping notification.")
        return
    try:
        requests.post(_URL, json={"chat_id": _CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=5)
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)


def signal_detected(signal_id: int, zone_class: str, zone_type: str,
                    timeframe: str, entry: float, sl: float,
                    target: float, score: float, confluence: str):
    emoji = "🟢" if zone_class == "demand" else "🔴"
    _send(
        f"{emoji} <b>New Signal #{signal_id}</b>\n"
        f"Zone: {zone_class.upper()} {zone_type} | {timeframe}\n"
        f"Entry: {entry:.2f} | SL: {sl:.2f} | Target: {target:.2f}\n"
        f"Score: {score:.1f}/10 | Confluence: {confluence}\n"
        f"👉 Open dashboard to Approve or Reject"
    )


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


def eod_summary(trades: int, wins: int, losses: int, total_pnl: float):
    pnl_str = f"+{total_pnl:.2f}" if total_pnl >= 0 else f"{total_pnl:.2f}"
    emoji = "📈" if total_pnl >= 0 else "📉"
    _send(
        f"{emoji} <b>EOD Summary</b>\n"
        f"Trades: {trades} | Wins: {wins} | Losses: {losses}\n"
        f"Net P&L: {pnl_str} pts"
    )
