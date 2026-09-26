import pytest
from tests.test_execution_lifecycle import env
from brokers.base import ExecutionFill, OrderExecution
from engine import execution as ex
from journal import db


def test_guard_compares_rupees_at_boundary(env):
    sid, broker, settings, _ = env
    ex.submit_entry(sid, broker)
    broker.initial_status = 'OPEN'
    ex.request_exit(sid, broker=broker)
    broker.set_snapshot('2', 'SELL', 'COMPLETE', [(10, 50)])
    ex.reconcile_trade(sid, broker)
    for order in db.execution_orders(sid):
        db.record_actual_charges(order['id'], 0, 'note', str(order['id']))
    settings.update(CAPITAL=50000, MAX_RISK_PCT=.01)
    assert db.daily_rupee_accounting()['net'] == -500
    with pytest.raises(ValueError, match='Daily loss'):
        ex.check_daily_loss()
    settings['CAPITAL'] = 50001
    ex.check_daily_loss()


def test_partial_realization_and_ist_date(env):
    sid, broker, _, _ = env
    ex.submit_entry(sid, broker)
    broker.initial_status = 'OPEN'
    ex.request_exit(sid, broker=broker)
    broker.set_snapshot('2', 'SELL', 'OPEN', [(4, 50)], timestamp='2026-09-25T20:00:00+00:00')
    ex.reconcile_trade(sid, broker)
    assert db.daily_rupee_accounting('2026-09-26')['gross'] == -200
    assert db.daily_rupee_accounting('2026-09-25')['gross'] == 0
    assert db.daily_rupee_accounting('2026-09-26', mode='paper')['gross'] == 0


def test_missing_timestamp_blocks_guard(env):
    sid, broker, _, _ = env
    ex.submit_entry(sid, broker)
    broker.initial_status = 'OPEN'
    ex.request_exit(sid, broker=broker)
    broker.snapshots['2'] = OrderExecution('2', 'COMPLETE', 10,
        (ExecutionFill('s', 10, 90, None, 'unknown'),), 'SELL', 'TEST-PE', 10)
    ex.reconcile_trade(sid, broker)
    with pytest.raises(ValueError, match='incomplete'):
        ex.check_daily_loss()


def test_unverified_legacy_trade_is_not_zero(env):
    sid, _, _, _ = env
    with db._conn() as con:
        con.execute("UPDATE signals SET status='closed' WHERE id=?", (sid,))
    assert not db.daily_rupee_accounting()['complete']
    with pytest.raises(ValueError, match='incomplete'):
        ex.check_daily_loss()


def test_manual_and_automatic_requests_share_the_loss_guard(env):
    sid, broker, settings, _ = env
    ex.submit_entry(sid, broker)
    broker.initial_status = 'OPEN'
    ex.request_exit(sid, broker=broker)
    broker.set_snapshot('2', 'SELL', 'COMPLETE', [(10, 50)])
    ex.reconcile_trade(sid, broker)
    settings['CAPITAL'] = 10000
    source = dict(db.get_signal(sid))
    source.update(total=10, status='pending')
    new_sid = db.log_signal(source | {'mode': 'live'})
    for requester in ('scheduler', 'dashboard', 'telegram'):
        with pytest.raises(ValueError, match='Daily loss'):
            ex.submit_entry(new_sid, broker, requester=requester)
    assert len(broker.orders) == 2
