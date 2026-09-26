import pytest
from tests.test_execution_lifecycle import env
from engine import execution as ex
from journal import db


def test_actual_net_only_after_both_order_charges_reconciled(env):
    sid, broker, _, _ = env
    ex.submit_entry(sid, broker)
    ex.request_exit(sid, broker=broker)
    buy, sell = db.execution_orders(sid)
    db.record_actual_charges(buy['id'], 25, 'contract_note', 'note-1-buy')
    assert db.get_signal(sid)['options_net_pnl_rs'] is None
    db.record_actual_charges(sell['id'], 30, 'contract_note', 'note-1-sell')
    row = db.get_signal(sid)
    assert row['options_gross_pnl_rs'] == 100
    assert row['options_charges_rs'] == 55
    assert row['options_net_pnl_rs'] == 45
    db.record_actual_charges(sell['id'], 30, 'contract_note', 'note-1-sell')
    assert db.get_signal(sid)['options_net_pnl_rs'] == 45


def test_gross_independent_of_zone_direction_and_lot_fallback(env):
    sid, broker, _, _ = env
    with db._conn() as con:
        con.execute("UPDATE signals SET zone_class='demand', stop_loss=90, intraday_target=120 WHERE id=?", (sid,))
    ex.submit_entry(sid, broker)
    with db._conn() as con:
        con.execute('UPDATE signals SET options_lot_size=NULL WHERE id=?', (sid,))
    ex.request_exit(sid, broker=broker)
    assert db.get_signal(sid)['options_gross_pnl_rs'] == 100


def test_duplicate_fill_reconciliation_is_idempotent(env):
    sid, broker, _, _ = env
    broker.initial_status = 'OPEN'
    ex.submit_entry(sid, broker)
    broker.set_snapshot('1', 'BUY', 'OPEN', [(4, 100)])
    ex.reconcile_trade(sid, broker)
    ex.reconcile_trade(sid, broker)
    assert db.get_signal(sid)['options_entry_quantity'] == 4


def test_multiple_entry_prices_use_fifo(env):
    sid, broker, _, _ = env
    broker.initial_status = 'OPEN'
    ex.submit_entry(sid, broker)
    broker.set_snapshot('1', 'BUY', 'COMPLETE', [(4, 100), (6, 120)])
    ex.reconcile_trade(sid, broker)
    ex.request_exit(sid, broker=broker)
    broker.set_snapshot('2', 'SELL', 'OPEN', [(3, 110)])
    row = ex.reconcile_trade(sid, broker)
    assert row['options_gross_pnl_rs'] == 30
    broker.set_snapshot('2', 'SELL', 'COMPLETE', [(3, 110), (7, 130)])
    row = ex.reconcile_trade(sid, broker)
    assert row['options_gross_pnl_rs'] == 120


def test_invalid_actual_charges_rejected(env):
    for value in (-1, float('nan'), float('inf')):
        with pytest.raises(ValueError):
            db.record_actual_charges(1, value, 'note', 'id')


def test_charge_estimate_is_per_order_and_includes_statutory_costs():
    from agents.costs import estimate_order_cost
    assert estimate_order_cost('BUY', 10000) == 28.10
    assert estimate_order_cost('SELL', 10000) == 42.80
    assert estimate_order_cost('BUY', 0) == 0
    with pytest.raises(ValueError):
        estimate_order_cost('SELL', float('nan'))
