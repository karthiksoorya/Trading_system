import sqlite3
from pathlib import Path

import journal.db as db


def test_fill_context_columns_are_migrated_and_recorded(tmp_path, monkeypatch):
    database = tmp_path / "trades.db"
    monkeypatch.setattr(db.config, "DB_PATH", database)
    monkeypatch.setattr(db.config, 'load_settings', lambda: {'MODE': 'paper'})
    db.init_db()
    signal = {
        "zone_type":"DBD", "zone_class":"supply", "timeframe":"5minute",
        "proximal":100, "distal":110, "entry":100, "stop_loss":115,
        "intraday_target":90, "overnight_target":90, "total":8,
        "freshness":2, "strength":2, "time_score":2, "rr_score":2,
        "entry_type":1, "position_size":1, "confluence_count":1, "confluence_tfs":None,
    }
    signal_id = db.log_signal(signal)
    db.update_signal_entry_price(signal_id, 101.25)
    row = db.get_signal(signal_id)
    assert row["options_entry_price"] == 101.25
    assert row["option_fill_time"] is None
    assert row["signal_to_fill_seconds"] is None
    assert row["underlying_at_option_fill"] is None
    assert row["underlying_fill_basis"] is None


def test_fill_logging_failure_does_not_raise(monkeypatch):
    class Broken:
        def __enter__(self): raise RuntimeError("logging unavailable")
        def __exit__(self, *args): return False
    monkeypatch.setattr(db, "_conn", lambda: Broken())
    db.update_signal_entry_price(1, 100)


from tests.test_execution_lifecycle import env
from brokers.base import ExecutionFill, OrderExecution
from engine import execution as ex


def test_broker_timestamp_is_preserved_across_midnight(env):
    sid, broker, _, _ = env
    broker.initial_status = 'OPEN'
    ex.submit_entry(sid, broker)
    with db._conn() as con:
        con.execute("UPDATE signals SET date='2026-09-25', time_signal='23:59:50' WHERE id=?", (sid,))
    broker.set_snapshot('1', 'BUY', 'COMPLETE', [(10, 100)], timestamp='2026-09-25T18:30:10Z')
    row = ex.reconcile_trade(sid, broker)
    assert row['option_fill_time'] == '2026-09-26T00:00:10+05:30'
    assert row['signal_to_fill_seconds'] == 20
    assert row['option_fill_time_basis'] == 'broker_exchange_execution'
    assert row['underlying_at_option_fill'] is None  # No historical underlying quote.


def test_missing_broker_timestamp_not_replaced_by_local_clock(env):
    sid, broker, _, _ = env
    broker.initial_status = 'OPEN'
    ex.submit_entry(sid, broker)
    broker.snapshots['1'] = OrderExecution('1', 'COMPLETE', 10,
        (ExecutionFill('b1', 10, 100, None, 'unknown'),), 'BUY', 'TEST-PE', 10)
    row = ex.reconcile_trade(sid, broker)
    assert row['option_fill_time'] is None
    assert row['signal_to_fill_seconds'] is None
    assert row['underlying_at_option_fill'] is None


def test_migration_preserves_unknowns_and_legacy_values(env):
    sid, _, _, _ = env
    with db._conn() as con:
        con.execute("UPDATE signals SET status='closed', option_fill_time='10:00:00', options_entry_price=100 WHERE id=?", (sid,))
        con.execute('DROP TABLE execution_charges')
        con.execute('DROP TABLE execution_fills')
        con.execute('DROP TABLE execution_orders')
    db.init_db()
    db.init_db()
    row = db.get_signal(sid)
    assert row['status'] == 'closed'
    assert row['option_fill_time'] == '10:00:00'
    assert row['option_fill_time_basis'] == 'legacy_local_clock'
    for col in ('options_entry_quantity', 'options_exit_quantity', 'options_charges_rs',
                'options_net_pnl_rs', 'underlying_at_option_fill', 'option_exit_fill_time'):
        assert row[col] is None
    assert db.execution_orders(sid) == []


def test_pre_execution_schema_upgrade_is_additive(env, tmp_path, monkeypatch):
    sid, _, _, _ = env
    original = dict(db.get_signal(sid))
    original['status'] = 'closed'
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as con:
        con.execute(db._CREATE_SIGNALS)
        con.execute(db._CREATE_DAILY)
        columns = [r[1] for r in con.execute('PRAGMA table_info(signals)')]
        con.execute(f"INSERT INTO signals ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    [original.get(c) for c in columns])
    monkeypatch.setattr(db.config, 'DB_PATH', path)
    db.init_db()
    db.init_db()
    migrated = dict(db.get_signal(sid))
    assert all(migrated[c] == original.get(c) for c in columns)
    for col in ('option_fill_time', 'underlying_at_option_fill', 'options_entry_quantity',
                'options_exit_quantity', 'options_charges_rs', 'options_net_pnl_rs'):
        assert migrated[col] is None
