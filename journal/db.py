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
        _migrate_execution(con)
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
        ("option_fill_time", "TEXT"),
        ("underlying_at_option_fill", "REAL"),
        ("underlying_fill_basis", "TEXT"),
        ("signal_to_fill_seconds", "REAL"),
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
            try:
                con.execute(f"ALTER TABLE signals ADD COLUMN {col} {definition}")
                logger.info("DB migration: added column %s", col)
            except Exception as e:
                if "duplicate column" in str(e).lower():
                    pass  # already exists — concurrent init or re-run
                else:
                    raise
    # Fix any rows closed before status='closed' was introduced
    con.execute(
        "UPDATE signals SET status='closed' WHERE status='approved' AND exit_price IS NOT NULL AND mode!='live'"
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
                departure_strength, base_compression, vix_at_signal, iv_rank_at_signal,
                mode
            ) VALUES (
                :date, :time_signal, :zone_type, :zone_class, :timeframe,
                :proximal, :distal, :entry, :stop_loss,
                :intraday_target, :overnight_target,
                :total, :freshness, :strength, :time_score, :rr_score,
                :entry_type, :position_size,
                :confluence_count, :confluence_tfs,
                :departure_strength, :base_compression, :vix_at_signal, :iv_rank_at_signal,
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
                "iv_rank_at_signal":  signal_data.get("iv_rank_at_signal"),
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
    options_exit_price: float | None = None,
):
    """Update a signal row when the trade closes.
    closed_by: 'system' (target/SL), 'telegram', 'dashboard', 'eod'
    """
    entry_row = get_signal(signal_id)
    if not entry_row:
        logger.warning("Signal id=%s not found.", signal_id)
        return

    if entry_row["mode"] == "live":
        raise ValueError("Live trades close only through broker execution reconciliation")

    # BUG 10 fix: guard against double-close (race condition between monitor + Telegram/dashboard)
    if entry_row["status"] == "closed":
        logger.warning("Signal id=%s already closed — skipping duplicate close.", signal_id)
        return

    entry     = entry_row["entry"]
    zone_class = entry_row["zone_class"]
    pnl_points = (exit_price - entry) if zone_class == "demand" else (entry - exit_price)
    result = "win" if pnl_points > 0 else ("loss" if pnl_points < 0 else "breakeven")

    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        if con.execute("SELECT status FROM signals WHERE id=?", (signal_id,)).fetchone()[0] == "closed":
            return
        con.execute(
            """
            UPDATE signals
            SET status='closed', exit_time=?, exit_price=?, exit_reason=?,
                pnl_points=?, result=?, notes=?, closed_by=?,
                options_exit_price=COALESCE(?, options_exit_price),
                options_exit_quantity=COALESCE(options_entry_quantity, options_exit_quantity)
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
                options_exit_price,
                signal_id,
            ),
        )

        _upsert_daily_summary(entry_row["date"], pnl_points, result, con)
    logger.info("Trade closed: id=%s result=%s pnl=%.2f pts", signal_id, result, pnl_points)


def _upsert_daily_summary(trade_date: str, pnl_points: float, result: str, connection=None):
    from contextlib import nullcontext
    with (nullcontext(connection) if connection is not None else _conn()) as con:
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
    try:
        with _conn() as con:
            con.execute(
                "UPDATE signals SET options_entry_price=? WHERE id=? AND mode!='live'",
                (options_entry_price, signal_id),
            )
    except Exception as exc:
        # Observational logging must never interrupt an already-confirmed BUY.
        logger.warning("Could not record option fill context for signal %s: %s", signal_id, exc)


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
            "UPDATE signals SET options_exit_order_id=?, options_exit_price=? WHERE id=? AND mode!='live'",
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
            "SELECT * FROM signals WHERE status IN ('approved', 'entry_pending', 'exit_pending', 'reconciliation_required')"
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
            "UPDATE signals SET status = 'approved' WHERE id = ? AND status='pending' AND mode!='live'",
            (signal_id,),
        )
    logger.info("Signal #%d approved.", signal_id)


def reject_signal(signal_id: int, note: str = ""):
    """User rejected the signal — skip it. Pass note for auto-rejections."""
    with _conn() as con:
        con.execute(
            "UPDATE signals SET status = 'rejected', notes = COALESCE(NULLIF(?, ''), notes) WHERE id = ? AND status='pending'",
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
    """Confirmed realized gross option rupees; incomplete data is never zero."""
    result = daily_rupee_accounting(trade_date, mode=config.load_settings().get('MODE', config.MODE))
    return result['gross'] if result['complete'] else None


# Execution ledger. Existing point-based research fields retain their meaning.
def _migrate_execution(con):
    columns = {
        "execution_broker": "TEXT", "execution_state": "TEXT",
        "option_fill_time_basis": "TEXT", "option_exit_fill_time": "TEXT",
        "underlying_observed_at": "TEXT", "options_entry_quantity": "INTEGER",
        "options_exit_quantity": "INTEGER", "options_gross_pnl_rs": "REAL",
        "options_charges_rs": "REAL", "options_net_pnl_rs": "REAL",
        "options_estimated_charges_rs": "REAL", "options_estimated_net_pnl_rs": "REAL",
        "accounting_status": "TEXT",
    }
    existing = {r[1] for r in con.execute("PRAGMA table_info(signals)")}
    for name, kind in columns.items():
        if name not in existing:
            con.execute(f"ALTER TABLE signals ADD COLUMN {name} {kind}")
    con.execute("UPDATE signals SET option_fill_time_basis='legacy_local_clock' "
                "WHERE option_fill_time IS NOT NULL AND option_fill_time_basis IS NULL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS execution_orders (
            id INTEGER PRIMARY KEY, signal_id INTEGER NOT NULL REFERENCES signals(id),
            broker TEXT NOT NULL, broker_order_id TEXT, side TEXT NOT NULL,
            requested_quantity INTEGER NOT NULL, filled_quantity INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL, requested_at TEXT NOT NULL, confirmed_at TEXT,
            exit_reason TEXT, requester TEXT, error TEXT,
            UNIQUE(broker, broker_order_id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_unresolved_order_per_signal
            ON execution_orders(signal_id) WHERE status NOT IN ('COMPLETE','CANCELLED','REJECTED');
        CREATE TABLE IF NOT EXISTS execution_fills (
            id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES execution_orders(id),
            broker TEXT NOT NULL, execution_id TEXT NOT NULL, quantity INTEGER NOT NULL,
            premium REAL NOT NULL, broker_timestamp TEXT, timestamp_basis TEXT NOT NULL,
            underlying_price REAL, underlying_observed_at TEXT, underlying_basis TEXT,
            UNIQUE(order_id, execution_id)
        );
        CREATE TABLE IF NOT EXISTS execution_charges (
            order_id INTEGER PRIMARY KEY REFERENCES execution_orders(id),
            amount REAL NOT NULL, source TEXT NOT NULL, reference TEXT NOT NULL,
            reconciled_at TEXT NOT NULL, basis TEXT NOT NULL CHECK(basis='actual')
        );
    """)


def execution_orders(signal_id):
    with _conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM execution_orders WHERE signal_id=? ORDER BY id", (signal_id,))]


def _accounting(con, signal_id):
    """FIFO realized proceeds and allocated order charges; no guessed quantity."""
    from agents.costs import estimate_order_cost
    orders = {r['id']: dict(r) for r in con.execute(
        "SELECT * FROM execution_orders WHERE signal_id=? ORDER BY id", (signal_id,))}
    fills = [dict(r) for r in con.execute(
        "SELECT f.* FROM execution_fills f JOIN execution_orders o ON o.id=f.order_id "
        "WHERE o.signal_id=? ORDER BY o.id, f.broker_timestamp, f.id", (signal_id,))]
    charges = {r['order_id']: r['amount'] for r in con.execute(
        "SELECT c.* FROM execution_charges c JOIN execution_orders o ON o.id=c.order_id WHERE o.signal_id=?",
        (signal_id,))}
    totals = {oid: sum(f['quantity'] for f in fills if f['order_id'] == oid) for oid in orders}
    estimates = {oid: estimate_order_cost(o['side'],
                 sum(f['quantity'] * f['premium'] for f in fills if f['order_id'] == oid))
                 for oid, o in orders.items()}
    buys, realized = [], []
    complete = all(totals[oid] == o['filled_quantity'] for oid, o in orders.items())
    for f in fills:
        if orders[f['order_id']]['side'] == 'BUY':
            buys.append([f, f['quantity']])
            continue
        remaining = f['quantity']
        for buy in buys:
            if not remaining:
                break
            entry, available = buy
            q = min(available, remaining)
            if not q:
                continue
            buy[1] -= q
            remaining -= q
            bo, so = entry['order_id'], f['order_id']
            actual = (charges[bo] * q / totals[bo] + charges[so] * q / totals[so]
                      if bo in charges and so in charges else None)
            estimated = ((charges.get(bo, estimates[bo]) * q / totals[bo]) +
                         (charges.get(so, estimates[so]) * q / totals[so]))
            gross = (f['premium'] - entry['premium']) * q
            realized.append(dict(gross=gross, charges=actual, estimated_charges=estimated,
                                 timestamp=f['broker_timestamp']))
        if remaining:
            complete = False
    return orders, fills, realized, complete


def refresh_accounting(con, signal_id):
    orders, fills, realized, complete = _accounting(con, signal_id)
    buys = [f for f in fills if orders[f['order_id']]['side'] == 'BUY']
    sells = [f for f in fills if orders[f['order_id']]['side'] == 'SELL']
    bq, sq = sum(f['quantity'] for f in buys), sum(f['quantity'] for f in sells)
    gross = sum(r['gross'] for r in realized) if realized and complete else None
    charges = sum(r['charges'] for r in realized) if realized and complete and all(r['charges'] is not None for r in realized) else None
    estimated = sum(r['estimated_charges'] for r in realized) if gross is not None else None
    state = 'actual' if charges is not None else ('estimated_charges' if gross is not None else 'pending')
    if not complete:
        state = 'incomplete'
    con.execute("""UPDATE signals SET options_entry_quantity=?, options_exit_quantity=?,
        options_entry_price=?, options_exit_price=?, options_gross_pnl_rs=?, options_charges_rs=?,
        options_net_pnl_rs=?, options_estimated_charges_rs=?, options_estimated_net_pnl_rs=?,
        accounting_status=? WHERE id=?""",
        (bq, sq, sum(f['quantity'] * f['premium'] for f in buys) / bq if bq else None,
         sum(f['quantity'] * f['premium'] for f in sells) / sq if sq else None,
         round(gross, 2) if gross is not None else None, round(charges, 2) if charges is not None else None,
         round(gross - charges, 2) if charges is not None else None,
         round(estimated, 2) if estimated is not None else None,
         round(gross - estimated, 2) if estimated is not None else None, state, signal_id))
    return bq, sq, complete


def record_actual_charges(order_id, amount, source, reference):
    """Reconcile an entire order's confirmed contract-note charges, not a prediction."""
    import math
    from brokers.base import IST
    if not math.isfinite(amount) or amount < 0 or not source or not reference:
        raise ValueError("Actual charges require a finite amount and source/reference")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        order = con.execute("SELECT * FROM execution_orders WHERE id=?", (order_id,)).fetchone()
        if not order or order['status'] not in ('COMPLETE', 'CANCELLED', 'REJECTED'):
            raise ValueError("Reconcile charges only for a terminal order")
        con.execute("INSERT INTO execution_charges VALUES (?,?,?,?,?,'actual') "
                    "ON CONFLICT(order_id) DO UPDATE SET amount=excluded.amount, source=excluded.source, "
                    "reference=excluded.reference, reconciled_at=excluded.reconciled_at",
                    (order_id, amount, source, reference, datetime.now(IST).isoformat()))
        refresh_accounting(con, order['signal_id'])


def daily_rupee_accounting(trade_date=None, mode='live'):
    from brokers.base import IST
    trade_date = trade_date or datetime.now(IST).date().isoformat()
    gross = net = 0.0
    complete, estimated = True, False
    with _conn() as con:
        for row in con.execute("SELECT * FROM signals WHERE mode=?", (mode,)).fetchall():
            orders, fills, realized, valid = _accounting(con, row['id'])
            # Unverified legacy rows today and unresolved exposure cannot become zero P&L.
            if not orders and ((row['date'] == trade_date and row['status'] in ('approved','closed'))
                               or row['status'] in ('approved','entry_pending','exit_pending','reconciliation_required')):
                complete = False
            if not valid or any(o['status'] in ('UNKNOWN', 'SUBMITTING') for o in orders.values()):
                complete = False
            for r in realized:
                if r['timestamp'] is None:
                    complete = False
                    continue
                if r['timestamp'][:10] != trade_date:
                    continue
                gross += r['gross']
                fee = r['charges'] if r['charges'] is not None else r['estimated_charges']
                estimated |= r['charges'] is None
                net += r['gross'] - fee
    return dict(gross=round(gross, 2), net=round(net, 2), complete=complete,
                basis='estimated_charges' if estimated else 'actual')
