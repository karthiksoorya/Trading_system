"""
Backup trades.db by sending it as a file to your Telegram chat.

Runs automatically at 15:45 IST every trading day.
Also triggered by the 'Backup Now' button in the dashboard.
"""

import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler

import requests

import config

LOG_DIR  = config.BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "backup.log"

LOG_DIR.mkdir(exist_ok=True)
_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
logger = logging.getLogger(__name__)
logger.addHandler(_handler)
logger.setLevel(logging.INFO)


def run_backup() -> bool:
    """Send trades.db to Telegram chat. Returns True on success."""
    import notify

    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        msg = "Backup skipped — TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set."
        logger.warning(msg)
        return False

    if not config.DB_PATH.exists():
        msg = "Backup skipped — trades.db not found."
        logger.warning(msg)
        notify.backup_result(success=False, message=msg)
        return False

    try:
        now_str  = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"trades_{now_str}.db"
        caption  = f"📦 trades.db backup — {datetime.now().strftime('%d %b %Y %H:%M')}"

        with open(config.DB_PATH, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data={"chat_id": chat_id, "caption": caption},
                files={"document": (filename, f, "application/octet-stream")},
                timeout=60,
            )

        if r.ok and r.json().get("ok"):
            msg = f"{filename} sent to Telegram."
            logger.info("Backup OK — %s", msg)
        else:
            msg = r.json().get("description", r.text)
            logger.error("Telegram sendDocument failed: %s", msg)
            notify.backup_result(success=False, message=msg)
            return False

        # Also back up agent memory if it exists
        memory_path = config.BASE_DIR / "agent" / "memory.json"
        if memory_path.exists():
            try:
                mem_filename = f"memory_{now_str}.json"
                with open(memory_path, "rb") as mf:
                    requests.post(
                        f"https://api.telegram.org/bot{token}/sendDocument",
                        data={"chat_id": chat_id, "caption": "🧠 agent/memory.json backup"},
                        files={"document": (mem_filename, mf, "application/json")},
                        timeout=30,
                    )
                logger.info("Memory backup sent — %s", mem_filename)
            except Exception as me:
                logger.warning("Memory backup failed (non-fatal): %s", me)

        notify.backup_result(success=True, message=msg)
        return True

    except Exception as e:
        logger.error("Backup error: %s", e)
        notify.backup_result(success=False, message=str(e))
        return False


if __name__ == "__main__":
    ok = run_backup()
    sys.exit(0 if ok else 1)
