"""
Polls Telegram for inline keyboard callbacks (Approve / Reject buttons).
Runs as a background daemon thread inside the scheduler process.
"""

import logging
import os
import threading
import time

import requests

logger = logging.getLogger(__name__)

_running = False
_offset  = 0


def start_polling():
    global _running
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        logger.info("Telegram not configured — callback polling skipped.")
        return
    _running = True
    threading.Thread(target=_poll_loop, daemon=True, name="tg-callback").start()
    logger.info("Telegram callback polling started.")


def stop_polling():
    global _running
    _running = False


def _poll_loop():
    global _offset
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    while _running:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={
                    "offset":          _offset,
                    "timeout":         0,
                    "allowed_updates": ["callback_query"],
                },
                timeout=10,
            )
            data = r.json()
            if data.get("ok"):
                for update in data["result"]:
                    _offset = update["update_id"] + 1
                    cb = update.get("callback_query")
                    if cb:
                        _handle_callback(cb, token)
        except Exception as e:
            logger.debug("Telegram poll error: %s", e)
        time.sleep(5)


def _handle_callback(cb: dict, token: str):
    cb_id  = cb["id"]
    action = cb.get("data", "")
    answer = "⚠️ Unknown action"

    try:
        if action.startswith("approve_"):
            sig_id = int(action.split("_", 1)[1])
            from journal.db import get_open_trades, approve_signal, get_signal
            import notify
            if get_open_trades():
                answer = "⚠️ Already have an open trade — reject it first."
            else:
                approve_signal(sig_id)
                answer = f"✅ Signal #{sig_id} approved!"
                logger.info("Telegram approved signal #%d", sig_id)
                row = get_signal(sig_id)
                if row:
                    notify.trade_approved(sig_id, row["entry"], row["stop_loss"], row["intraday_target"])

        elif action.startswith("reject_"):
            sig_id = int(action.split("_", 1)[1])
            from journal.db import reject_signal
            reject_signal(sig_id)
            answer = f"❌ Signal #{sig_id} rejected."
            logger.info("Telegram rejected signal #%d", sig_id)

        elif action.startswith("close_"):
            trade_id = int(action.split("_", 1)[1])
            from journal.db import get_open_trades, close_trade
            import notify
            import config
            open_trades = get_open_trades()
            match = next((dict(r) for r in open_trades if r["id"] == trade_id), None)
            if not match:
                answer = f"⚠️ Trade #{trade_id} is not open."
            else:
                try:
                    from brokers import get_broker
                    ltp = get_broker().get_ltp(config.NIFTY_SYMBOL)
                except Exception:
                    ltp = match["entry"]
                pnl = round(
                    (ltp - match["entry"]) if match["zone_class"] == "demand"
                    else (match["entry"] - ltp), 2
                )
                close_trade(trade_id, ltp, "manual")
                notify.trade_closed(trade_id, ltp, "manual", pnl)
                answer = f"🚨 Trade #{trade_id} closed manually at {ltp:.2f} | P&L: {pnl:+.2f} pts"
                logger.info("Telegram manually closed trade #%d at %.2f", trade_id, ltp)

    except Exception as e:
        answer = f"⚠️ Error: {e}"
        logger.warning("Telegram callback error: %s", e)

    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/answerCallbackQuery",
            json={"callback_query_id": cb_id, "text": answer, "show_alert": False},
            timeout=5,
        )
    except Exception as e:
        logger.debug("answerCallbackQuery failed: %s", e)
