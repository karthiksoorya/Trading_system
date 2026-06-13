import sqlite3
import logging
from datetime import date, datetime
from contextlib import contextmanager
from typing import Optional

import config

logger = logging.getLogger(__name__)

# ── Schema ────────────────────────────────────────────────────────────────
# Matches the Paper Trade CSV schema from the master doc.

_CREATE_SIGNALS = """
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    date            TEXT NOT NULL,
    time_signal     TEXT NOT NULL,
    zone_type       TEXT NOT NULL,   -- DBR | RBR | RBD | DBD
    zone_class      TEXT NOT NULL,   -- demand | supply
    timeframe       TEXT NOT NULL,
    proximal        REAL NOT NULL,
    distal          REAL NOT NULL,
    entry           REAL NOT NULL,
    stop_loss       REAL NOT NULL,
    intraday_target REAL NOT NULL,
    overnight_target REAL,
    booster_score   REAL NOT NULL,
    freshness       REAL NOT NULL,
    strength        REAL NOT NULL,
    time_score      REAL NOT NULL,
    rr_score        REAL NOT NULL,
    entry_type        INTEGER NOT NULL,
    position_size     REAL NOT NULL,
    confluence_count  INTEGER DEFAULT 1,  -- number of TFs in agreement
    confluence_tfs    TEXT,               -- e.g. "5minute + 15minute + 60minute"
    -- filled after trade closes
    exit_time       TEXT,
    exit_price      REAL,
    exit_reason     TEXT,            -- target | stoploss | manual | eod
    pnl_points      REAL,
    result          TEXT,            -- win | loss | breakeven
    rule_based      INTEGER DEFAULT 1,  -- 1=yes 0=no
    notes           TEXT,
    mode            TEXT DEFAULT 'paper'
)
"""

_CREATE_DAILY = """
CREATE TABLE IF NOT EXISTS daily_summary (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT UNIQUE NOT NULL,
    trades_taken    INTEGER DEFAULT 0,
    wins            INTEGER DEFAULT 0,
    losses          INTEGER DEFAULT 0,
    total_pnl       REAL DEFAULT 0,
    max_daily_loss  REAL,
    notes           TEXT
)
"""


# ── Connection helper ──────────────────────────────────────────────────────

@contextmanager
def _conn():
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ── Init ──────────────────────────────────────────────────────────────────

def init_db():
    with _conn() as con:
        con.execute(_CREATE_SIGNALS)
        con.execute(_CREATE_DAILY)
        _migrate(con)
    logger.info("Database initialised at %s", config.DB_PATH)


def _migrate(con):
    """Add new columns to existing DB without breaking old data."""
    existing = {row[1] for row in con.execute("PRAGMA table_info(signals)")}
    migrations = [
        ("confluence_count", "INTEGER DEFAULT 1"),
        ("confluence_tfs",   "TEXT"),
        ("status",           "TEXT NOT NULL DEFAULT 'pending'"),
    ]
    for col, definition in migrations:
        if col not in existing:
            con.execute(f"ALTER TABLE signals ADD COLUMN {col} {definition}")
            logger.info("DB migration: added column %s", col)


# ── Write ─────────────────────────────────────────────────────────────────

def log_signal(signal_data: dict) -> int:
    """Insert a new signal row. Returns the new row id."""
    now = datetime.now()
    with _conn() as con:
        cur = con.execute(
            """
            INSERT INTO signals (
                date, time_signal, zone_type, zone_class, timeframe,
                proximal, distal, entry, stop_loss,
                intraday_target, overnight_target,
                booster_score, freshness, strength, time_score, rr_score,
                entry_type, position_size,
                confluence_count, confluence_tfs,
                mode
            ) VALUES (
                :date, :time_signal, :zone_type, :zone_class, :timeframe,
                :proximal, :distal, :entry, :stop_loss,
                :intraday_target, :overnight_target,
                :total, :freshness, :strength, :time_score, :rr_score,
                :entry_type, :position_size,
                :confluence_count, :confluence_tfs,
                :mode
            )
            """,
            {
                "date":            now.strftime("%Y-%m-%d"),
                "time_signal":     now.strftime("%H:%M:%S"),
                "mode":            config.MODE,
                **signal_data,
            },
        )
        return cur.lastrowid


def close_trade(
    signal_id: int,
    exit_price: float,
    exit_reason: str,
    notes: str = "",
):
    """Update a signal row when the trade closes."""
    entry_row = get_signal(signal_id)
    if not entry_row:
        logger.warning("Signal id=%s not found.", signal_id)
        return

    entry     = entry_row["entry"]
    zone_class = entry_row["zone_class"]
    pnl_points = (exit_price - entry) if zone_class == "demand" else (entry - exit_price)
    result = "win" if pnl_points > 0 else ("loss" if pnl_points < 0 else "breakeven")

    with _conn() as con:
        con.execute(
            """
            UPDATE signals
            SET exit_time=?, exit_price=?, exit_reason=?,
                pnl_points=?, result=?, notes=?
            WHERE id=?
            """,
            (
                datetime.now().strftime("%H:%M:%S"),
                exit_price,
                exit_reason,
                round(pnl_points, 2),
                result,
                notes,
                signal_id,
            ),
        )

    _upsert_daily_summary(entry_row["date"], pnl_points, result)
    logger.info("Trade closed: id=%s result=%s pnl=%.2f pts", signal_id, result, pnl_points)


def _upsert_daily_summary(trade_date: str, pnl_points: float, result: str):
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO daily_summary (date, max_daily_loss) VALUES (?, ?)",
            (trade_date, config.MAX_DAILY_LOSS),
        )
        con.execute(
            """
            UPDATE daily_summary SET
                trades_taken = trades_taken + 1,
                wins         = wins   + ?,
                losses       = losses + ?,
                total_pnl    = total_pnl + ?
            WHERE date = ?
            """,
            (
                1 if result == "win"  else 0,
                1 if result == "loss" else 0,
                round(pnl_points, 2),
                trade_date,
            ),
        )


# ── Read ──────────────────────────────────────────────────────────────────

def get_signal(signal_id: int) -> Optional[sqlite3.Row]:
    with _conn() as con:
        return con.execute(
            "SELECT * FROM signals WHERE id=?", (signal_id,)
        ).fetchone()


def get_signals_for_date(trade_date: Optional[str] = None) -> list[sqlite3.Row]:
    trade_date = trade_date or date.today().isoformat()
    with _conn() as con:
        return con.execute(
            "SELECT * FROM signals WHERE date=? ORDER BY time_signal",
            (trade_date,),
        ).fetchall()


def trades_today() -> int:
    """Count approved trades taken today (excludes pending and rejected)."""
    with _conn() as con:
        row = con.execute(
            "SELECT COUNT(*) FROM signals WHERE date=? AND status NOT IN ('pending', 'rejected')",
            (date.today().isoformat(),),
        ).fetchone()
        return row[0] if row else 0


def pending_count() -> int:
    """Number of today's signals waiting for user approval."""
    with _conn() as con:
        row = con.execute(
            "SELECT COUNT(*) FROM signals WHERE status = 'pending' AND date = ?",
            (date.today().isoformat(),),
        ).fetchone()
        return row[0] if row else 0


def get_pending_signals() -> list[sqlite3.Row]:
    """Today's signals awaiting approval, newest first."""
    with _conn() as con:
        return con.execute(
            "SELECT * FROM signals WHERE status = 'pending' AND date = ? ORDER BY id DESC",
            (date.today().isoformat(),),
        ).fetchall()


def expire_stale_pending():
    """Auto-reject any pending signals from previous days — they are no longer actionable."""
    with _conn() as con:
        count = con.execute(
            "UPDATE signals SET status = 'rejected' WHERE status = 'pending' AND date < ?",
            (date.today().isoformat(),),
        ).rowcount
    if count:
        logger.info("Expired %d stale pending signal(s) from previous days.", count)
    return count


def get_open_trades() -> list[sqlite3.Row]:
    """Approved trades that have not been closed yet."""
    with _conn() as con:
        return con.execute(
            "SELECT * FROM signals WHERE status = 'approved' AND exit_price IS NULL"
        ).fetchall()


def reject_all_pending():
    """Bulk-reject every pending signal (e.g. to clear stale test data)."""
    with _conn() as con:
        count = con.execute(
            "UPDATE signals SET status = 'rejected' WHERE status = 'pending'"
        ).rowcount
    logger.info("Bulk-rejected %d pending signal(s).", count)
    return count


def approve_signal(signal_id: int):
    """User approved the signal — mark as active trade."""
    with _conn() as con:
        con.execute(
            "UPDATE signals SET status = 'approved' WHERE id = ?",
            (signal_id,),
        )
    logger.info("Signal #%d approved.", signal_id)


def reject_signal(signal_id: int):
    """User rejected the signal — skip it."""
    with _conn() as con:
        con.execute(
            "UPDATE signals SET status = 'rejected' WHERE id = ?",
            (signal_id,),
        )
    logger.info("Signal #%d rejected.", signal_id)


def daily_pnl(trade_date: Optional[str] = None) -> float:
    trade_date = trade_date or date.today().isoformat()
    with _conn() as con:
        row = con.execute(
            "SELECT total_pnl FROM daily_summary WHERE date=?",
            (trade_date,),
        ).fetchone()
        return row["total_pnl"] if row else 0.0
