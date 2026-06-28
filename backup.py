"""
Google Drive backup for trades.db.

First-time setup (run once on your local machine):
    python backup.py --auth
    scp -i YOUR_KEY.pem gdrive_token.json ubuntu@13.201.210.4:~/Trading_system/

After that, backups run automatically at 15:45 every trading day,
or manually via the dashboard button.
"""

import json
import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import config

FOLDER_ID    = "1ZcoRt-IMkWWqD0VOFKLa1v2P7g9bwurR"
KEEP_BACKUPS = 7
SCOPES       = ["https://www.googleapis.com/auth/drive.file"]

CREDS_FILE   = config.BASE_DIR / "gdrive_credentials.json"
TOKEN_FILE   = config.BASE_DIR / "gdrive_token.json"
MANIFEST     = config.DATA_DIR / "backup_manifest.json"
LOG_DIR      = config.BASE_DIR / "logs"
LOG_FILE     = LOG_DIR / "backup.log"

LOG_DIR.mkdir(exist_ok=True)

_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
logger = logging.getLogger(__name__)
logger.addHandler(_handler)
logger.setLevel(logging.INFO)


# ── Auth ──────────────────────────────────────────────────────────────────

def auth():
    """OAuth flow — run once on local machine to generate gdrive_token.json."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Install deps first:  pip install google-api-python-client google-auth-oauthlib")
        sys.exit(1)

    if not CREDS_FILE.exists():
        print(f"\nERROR: {CREDS_FILE} not found.")
        print("Steps to get it:")
        print("  1. Go to https://console.cloud.google.com/")
        print("  2. Create project → Enable 'Google Drive API'")
        print("  3. APIs & Services → Credentials → Create OAuth 2.0 Client ID (Desktop app)")
        print("  4. Download JSON → save as gdrive_credentials.json in this folder")
        sys.exit(1)

    flow  = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json())
    print(f"\n✅ Token saved → {TOKEN_FILE}")
    print(f"\nNow copy it to VPS:")
    print(f"  scp -i YOUR_KEY.pem gdrive_token.json ubuntu@13.201.210.4:~/Trading_system/")


# ── Drive helpers ─────────────────────────────────────────────────────────

def _service():
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        raise RuntimeError("Run: pip install google-api-python-client google-auth-oauthlib")

    if not TOKEN_FILE.exists():
        raise RuntimeError("No gdrive_token.json — run: python backup.py --auth")

    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())

    return build("drive", "v3", credentials=creds)


def _load_manifest() -> list:
    try:
        return json.loads(MANIFEST.read_text())
    except Exception:
        return []


def _save_manifest(entries: list):
    MANIFEST.write_text(json.dumps(entries, indent=2))


# ── Main backup ───────────────────────────────────────────────────────────

def run_backup() -> bool:
    """Upload trades.db to Google Drive. Returns True on success."""
    import notify

    if not TOKEN_FILE.exists():
        msg = "Backup skipped — gdrive_token.json missing. Run: python backup.py --auth"
        logger.warning(msg)
        notify.backup_result(success=False, message=msg)
        return False

    if not config.DB_PATH.exists():
        msg = "Backup skipped — trades.db not found."
        logger.warning(msg)
        notify.backup_result(success=False, message=msg)
        return False

    try:
        from googleapiclient.http import MediaFileUpload

        svc      = _service()
        now_str  = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"trades_{now_str}.db"

        meta  = {"name": filename, "parents": [FOLDER_ID]}
        media = MediaFileUpload(str(config.DB_PATH), mimetype="application/octet-stream", resumable=False)
        f     = svc.files().create(body=meta, media_body=media, fields="id,name").execute()

        manifest = _load_manifest()
        manifest.append({"id": f["id"], "name": filename, "time": now_str})
        logger.info("Uploaded %s (id=%s)", filename, f["id"])

        # Rotate — delete oldest beyond KEEP_BACKUPS
        while len(manifest) > KEEP_BACKUPS:
            old = manifest.pop(0)
            try:
                svc.files().delete(fileId=old["id"]).execute()
                logger.info("Deleted old backup: %s", old["name"])
            except Exception as e:
                logger.warning("Could not delete %s: %s", old["name"], e)

        _save_manifest(manifest)

        msg = f"{filename} uploaded ({len(manifest)}/{KEEP_BACKUPS} slots used)"
        logger.info("Backup OK — %s", msg)
        notify.backup_result(success=True, message=msg)
        return True

    except Exception as e:
        msg = str(e)
        logger.error("Backup failed: %s", msg)
        notify.backup_result(success=False, message=msg)
        return False


if __name__ == "__main__":
    if "--auth" in sys.argv:
        auth()
    else:
        ok = run_backup()
        sys.exit(0 if ok else 1)
