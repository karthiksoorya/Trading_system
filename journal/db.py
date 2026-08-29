import sqlite3
import logging
from datetime import date, datetime, timedelta
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
        ("confluence_count",    "INTEGER DEFAULT 1"),
        ("confluence_tfs",      "TEXT"),
        ("status",              "TEXT NOT NULL DEFAULT 'pending'"),
        ("kite_order_id",       "TEXT"),
        ("options_symbol",      "TEXT"),
        ("mode",                "TEXT DEFAULT 'paper'"),
        ("options_entry_price", "REAL"),
        ("options_exit_price",  "REAL"),
        ("options_exit_order_id", "TEXT"),
        ("options_lot_size",    "INTEGER"),
        ("closed_by",           "TEXT"),
        ("sim_outcome",         "TEXT"),   # target | stoploss | eod — simulated for skipped signals
        ("sim_pnl_points",      "REAL"),   # simulated index P&L — for ML training
        ("departure_strength",  "REAL"),   # ATR departure at zone origin (x multiples)
        ("base_compression",    "REAL"),   # base candle compression ratio
        ("vix_at_signal",       "REAL"),   # India VIX at time of signal
        ("iv_rank_at_signal",   "REAL"),   # IV rank / percentile at time of signal
        ("agent_verdict",       "TEXT"),   # TRADE | SKIP | REVIEW — evaluator decision
        ("agent_reason",        "TEXT"),   # evaluator reason string
    ]
    for col, definition in migrations:
        if col not in existing:
            con.execute(f"ALTER TABLE signals ADD COLUMN {col} {definition}")
            logger.info("DB migration: added column %s", col)
    # Fix any rows closed before status='closed' was introduced
    con.execute(
        "UPDATE signals SET status='closed' WHERE status='approved' AND exit_price IS NOT NULL"
    )
    # BUG 21 fix: backfill any rows with NULL status that ALTER TABLE left behind
    con.execute("UPDATE signals SET status='pending' WHERE status IS NULL")
    # Backfill: any row without a mode tag was logged in paper mode
    con.execute("UPDATE signals SET mode='paper' WHERE mode IS NULL")


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
                departure_strength, base_compression, vix_at_signal,
                mode
            ) VALUES (
                :date, :time_signal, :zone_type, :zone_class, :timeframe,
                :proximal, :distal, :entry, :stop_loss,
                :intraday_target, :overnight_target,
                :total, :freshness, :strength, :time_score, :rr_score,
                :entry_type, :position_size,
                :confluence_count, :confluence_tfs,
                :departure_strength, :base_compression, :vix_at_signal,
                :mode
            )
            """,
            {
                "date":               now.strftime("%Y-%m-%d"),
                "time_signal":        now.strftime("%H:%M:%S"),
                "mode":               config.load_settings().get("MODE", config.MODE),  # BUG 4 fix
                "departure_strength": signal_data.get("departure_strength"),
                "base_compression":   signal_data.get("base_compression"),
                "vix_at_signal":      signal_data.get("vix_at_signal"),
                **signal_data,
            },
        )
        return cur.lastrowid


def update_signal_agent_verdict(signal_id: int, verdict: str, reason: str) -> None:
    """Log the agent's TRADE/SKIP/REVIEW verdict on a signal row."""
    with _conn() as con:
        con.execute(
            "UPDATE signals SET agent_verdict=?, agent_reason=? WHERE id=?",
            (verdict, reason, signal_id),
        )


def close_trade(
    signal_id: int,
    exit_price: float,
    exit_reason: str,
    notes: str = "",
    closed_by: str = "system",
):
    """Update a signal row when the trade closes.
    closed_by: 'system' (target/SL), 'telegram', 'dashboard', 'eod'
    """
    entry_row = get_signal(signal_id)
    if not entry_row:
        logger.warning("Signal id=%s not found.", signal_id)
        return

    # BUG 10 fix: guard against double-close (race condition between monitor + Telegram/dashboard)
    if entry_row["status"] == "closed":
        logger.warning("Signal id=%s already closed — skipping duplicate close.", signal_id)
        return

    entry     = entry_row["entry"]
    zone_class = entry_row["zone_class"]
    pnl_points = (exit_price - entry) if zone_class == "demand" else (entry - exit_price)
    result = "win" if pnl_points > 0 else ("loss" if pnl_points < 0 else "breakeven")

    with _conn() as con:
        con.execute(
            """
            UPDATE signals
            SET status='closed', exit_time=?, exit_price=?, exit_reason=?,
                pnl_points=?, result=?, notes=?, closed_by=?
            WHERE id=?
            """,
            (
                datetime.now().strftime("%H:%M:%S"),
                exit_price,
                exit_reason,
                round(pnl_points, 2),
                result,
                notes,
                closed_by,
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

def update_signal_order(signal_id: int, kite_order_id: str, options_symbol: str, lot_size: int = 0) -> None:
    """Store the live Kite BUY order details after entry order is placed."""
    with _conn() as con:
        con.execute(
            "UPDATE signals SET kite_order_id=?, options_symbol=?, options_lot_size=? WHERE id=?",
            (kite_order_id, options_symbol, lot_size or 0, signal_id),
        )


def update_signal_sl(signal_id: int, new_sl: float) -> None:
    """Move stop loss to a new level (used for breakeven SL)."""
    with _conn() as con:
        con.execute(
            "UPDATE signals SET stop_loss=? WHERE id=?",
            (new_sl, signal_id),
        )
    logger.info("Signal #%d SL updated to %.2f (breakeven)", signal_id, new_sl)


def update_signal_entry_price(signal_id: int, options_entry_price: float) -> None:
    """Store actual options premium paid after BUY order fills."""
    with _conn() as con:
        con.execute(
            "UPDATE signals SET options_entry_price=? WHERE id=?",
            (options_entry_price, signal_id),
        )


def update_signal_sim_outcome(signal_id: int, sim_outcome: str, sim_pnl_points: float) -> None:
    """Store simulated outcome for an expired/rejected signal. Used for ML training data."""
    with _conn() as con:
        con.execute(
            "UPDATE signals SET sim_outcome=?, sim_pnl_points=? WHERE id=?",
            (sim_outcome, round(sim_pnl_points, 2), signal_id),
        )


def update_signal_exit_order(signal_id: int, exit_order_id: str, exit_price: float) -> None:
    """Store actual options premium received after SELL order fills."""
    with _conn() as con:
        con.execute(
            "UPDATE signals SET options_exit_order_id=?, options_exit_price=? WHERE id=?",
            (exit_order_id, exit_price, signal_id),
        )


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
    """Count approved trades taken today (excludes pending, rejected, expired)."""
    with _conn() as con:
        row = con.execute(
            "SELECT COUNT(*) FROM signals WHERE date=? AND status NOT IN ('pending', 'rejected', 'expired')",
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


def zone_signaled_today(zone_class: str, zone_type: str, timeframe: str, proximal: float) -> bool:
    """Return True if this exact zone already has a signal logged today.

    BUG 19 fix: proximal is a REAL (float) in SQLite. Exact equality can fail due to
    floating-point epsilon differences between two separately-computed identical prices.
    Use a small tolerance band (±0.01 pts) instead of exact match.
    """
    with _conn() as con:
        row = con.execute(
            """SELECT id FROM signals
               WHERE date=? AND zone_class=? AND zone_type=? AND timeframe=?
               AND proximal BETWEEN ? AND ?
               AND status != 'rejected'
               LIMIT 1""",
            (date.today().isoformat(), zone_class, zone_type, timeframe,
             proximal - 0.01, proximal + 0.01),
        ).fetchone()
        return row is not None


def expire_signal(signal_id: int, note: str) -> None:
    """Expire a single pending signal with a custom reason note."""
    with _conn() as con:
        con.execute(
            "UPDATE signals SET status='expired', notes=? WHERE id=? AND status='pending'",
            (note, signal_id),
        )
    logger.info("Signal #%d auto-expired: %s", signal_id, note)


def expire_old_pending(expiry_minutes: int) -> int:
    """Auto-expire pending signals older than expiry_minutes. Returns count expired."""
    cutoff = (datetime.now() - timedelta(minutes=expiry_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as con:
        count = con.execute(
            """UPDATE signals SET status = 'expired',
                   notes = 'expired — zone no longer current'
               WHERE status = 'pending'
               AND (date || ' ' || time_signal) < ?""",
            (cutoff,),
        ).rowcount
    if count:
        logger.info("Expired %d pending signal(s) older than %d min.", count, expiry_minutes)
    return count


def get_open_trades() -> list[sqlite3.Row]:
    """Trades approved by user that are still active (not yet closed)."""
    with _conn() as con:
        return con.execute(
            "SELECT * FROM signals WHERE status = 'approved'"
        ).fetchall()


def reject_all_pending():
    """Bulk-reject all pending signals (any date)."""
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


def reject_signal(signal_id: int, note: str = ""):
    """User rejected the signal — skip it. Pass note for auto-rejections."""
    with _conn() as con:
        con.execute(
            "UPDATE signals SET status = 'rejected', notes = COALESCE(NULLIF(?, ''), notes) WHERE id = ?",
            (note, signal_id),
        )
    logger.info("Signal #%d rejected. %s", signal_id, note)


def daily_pnl(trade_date: Optional[str] = None) -> float:
    trade_date = trade_date or date.today().isoformat()
    with _conn() as con:
        row = con.execute(
            "SELECT total_pnl FROM daily_summary WHERE date=?",
            (trade_date,),
        ).fetchone()
        return row["total_pnl"] if row else 0.0


def daily_options_pnl(trade_date: Optional[str] = None) -> float:
    """Sum of (exit - entry) × lot_size for all closed options trades today.
    Returns 0.0 if no live options trades closed yet, or columns are NULL."""
    trade_date = trade_date or date.today().isoformat()
    with _conn() as con:
        rows = con.execute(
            """SELECT options_entry_price, options_exit_price, options_lot_size
               FROM signals
               WHERE date=? AND status='closed'
               AND options_entry_price IS NOT NULL
               AND options_exit_price  IS NOT NULL""",
            (trade_date,),
        ).fetchall()
    total = 0.0
    for r in rows:
        lot = r["options_lot_size"] or 65
        total += (r["options_exit_price"] - r["options_entry_price"]) * lot
    return round(total, 2)
