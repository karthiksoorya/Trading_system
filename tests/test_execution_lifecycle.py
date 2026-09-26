from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

import pytest

from brokers.base import IST, ExecutionFill, OrderExecution, UnderlyingObservation
from engine import execution as ex
from journal import db


class Broker:
    broker_name = 'kite'

    def __init__(self):
        self.orders = []
        self.snapshots = {}
        self.fail_submit = False
        self.initial_status = 'COMPLETE'
        self.quote_price = 100
        self.cancelled = []

    def is_connected(self):
        return True

    def get_options_contract(self, *args):
        return {'symbol': 'TEST-PE', 'lot_size': 10}

    def get_underlying_observation(self, symbol):
        return UnderlyingObservation(self.quote_price, datetime.now(IST).isoformat())

    def place_options_order(self, symbol, side, quantity, *, before_submit=None):
        if before_submit:
            before_submit()
        self.orders.append((symbol, side, quantity))
        if self.fail_submit:
            raise TimeoutError('response lost')
        oid = str(len(self.orders))
        self.set_snapshot(oid, side, self.initial_status,
                          [(quantity, 100 if side == 'BUY' else 110)] if self.initial_status == 'COMPLETE' else [],
                          quantity=quantity)
        return oid

    def set_snapshot(self, oid, side, status, fills, timestamp=None, quantity=10):
        stamp = timestamp or datetime.now(IST).isoformat()
        executions = tuple(ExecutionFill(f'{oid}-{i}', q, price, stamp) for i, (q, price) in enumerate(fills))
        self.snapshots[oid] = OrderExecution(oid, status, sum(f.quantity for f in executions), executions,
                                             side, 'TEST-PE', quantity)

    def get_order_execution(self, oid):
        return self.snapshots[oid]

    def cancel_execution_order(self, oid):
        self.cancelled.append(oid)
        old = self.snapshots[oid]
        self.snapshots[oid] = OrderExecution(old.order_id, 'CANCELLED', old.filled_quantity, old.fills,
                                             old.side, old.symbol, old.requested_quantity)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, '_poll_wait', lambda: None)
    monkeypatch.setattr(db.config, 'DB_PATH', tmp_path / 'trades.db')
    monkeypatch.setattr(db.config, 'CSV_DIR', tmp_path)
    settings = dict(MODE='live', BROKER='kite', CAPITAL=100000, MAX_RISK_PCT=.01,
                    MIN_REMAINING_RR=1, MIN_REMAINING_REWARD_FRACTION=.5,
                    ENTRY_QUOTE_MAX_AGE_SECONDS=5, SIGNAL_EXPIRY_MINUTES=45)
    monkeypatch.setattr(db.config, 'load_settings', lambda: settings)
    import notify
    messages = []
    monkeypatch.setattr(notify, '_send', messages.append)
    db.init_db()
    now = datetime.now(IST) - timedelta(seconds=30)
    signal = dict(date=now.date().isoformat(), time_signal=now.strftime('%H:%M:%S'),
                  zone_type='DBD', zone_class='supply', timeframe='5minute', proximal=100, distal=110,
                  entry=100, stop_loss=110, intraday_target=80, overnight_target=80,
                  total=10, freshness=2, strength=2, time_score=2, rr_score=2, entry_type=1,
                  position_size=1, confluence_count=1, confluence_tfs=None)
    sid = db.log_signal(signal)
    broker = Broker()
    return sid, broker, settings, messages


def test_complete_exit_uses_fills_and_notifies_once(env):
    sid, broker, _, messages = env
    entered = ex.submit_entry(sid, broker)
    assert entered['status'] == 'approved'
    assert entered['options_entry_quantity'] == 10
    assert entered['underlying_at_option_fill'] == 100
    assert entered['underlying_fill_basis'] == 'confirmation_quote'
    broker.quote_price = 90
    closed = ex.request_exit(sid, broker=broker)
    assert closed['status'] == 'closed'
    assert closed['options_gross_pnl_rs'] == 100
    assert closed['options_net_pnl_rs'] is None
    assert closed['options_estimated_net_pnl_rs'] < 100
    assert closed['option_exit_fill_time']
    ex.request_exit(sid, broker=broker)
    assert len(broker.orders) == 2
    assert len(messages) == 1
    assert db.daily_pnl() == 10


@pytest.mark.parametrize('status', ['OPEN', 'REJECTED', 'CANCELLED'])
def test_unfilled_exit_never_closes(env, status):
    sid, broker, _, messages = env
    ex.submit_entry(sid, broker)
    broker.initial_status = status
    row = ex.request_exit(sid, broker=broker)
    assert row['status'] != 'closed'
    assert row['options_exit_quantity'] == 0
    assert not messages


def test_timeout_does_not_resubmit(env):
    sid, broker, _, _ = env
    ex.submit_entry(sid, broker)
    broker.fail_submit = True
    row = ex.request_exit(sid, broker=broker)
    assert row['status'] == 'reconciliation_required'
    ex.request_exit(sid, broker=broker)
    assert len(broker.orders) == 2


def test_pending_exit_survives_restart_and_partial_fill(env):
    sid, broker, _, messages = env
    ex.submit_entry(sid, broker)
    broker.initial_status = 'OPEN'
    ex.request_exit(sid, broker=broker)
    broker.set_snapshot('2', 'SELL', 'OPEN', [(4, 110)])
    row = ex.reconcile_trade(sid, broker)
    assert row['status'] == 'exit_pending'
    assert row['options_gross_pnl_rs'] == 40
    db.init_db()
    broker.set_snapshot('2', 'SELL', 'COMPLETE', [(4, 110), (6, 120)])
    row = ex.reconcile_trade(sid, broker)
    assert row['status'] == 'closed'
    assert row['options_gross_pnl_rs'] == 160
    assert len(messages) == 1


def test_partial_buy_cancelled_before_exit(env):
    sid, broker, _, _ = env
    broker.initial_status = 'OPEN'
    ex.submit_entry(sid, broker)
    broker.set_snapshot('1', 'BUY', 'OPEN', [(4, 100)])
    ex.request_exit(sid, broker=broker)
    assert broker.cancelled == ['1']
    assert len(broker.orders) == 1
    broker.initial_status = 'COMPLETE'
    row = ex.request_exit(sid, broker=broker)
    assert broker.orders[-1][2] == 4
    assert row['status'] == 'closed'


def test_concurrent_exit_claim_is_unique(env):
    sid, broker, _, _ = env
    ex.submit_entry(sid, broker)
    broker.initial_status = 'OPEN'
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: ex.request_exit(sid, broker=broker), range(2)))
    assert len(broker.orders) == 2


def test_direct_live_close_and_wrong_broker_are_blocked(env):
    sid, broker, _, _ = env
    ex.submit_entry(sid, broker)
    with pytest.raises(ValueError, match='broker execution'):
        db.close_trade(sid, 90, 'manual')
    broker.broker_name = 'upstox'
    with pytest.raises(ValueError, match='does not match'):
        ex.request_exit(sid, broker=broker)


def test_mode_change_does_not_convert_live_exit_to_paper(env):
    sid, broker, settings, _ = env
    ex.submit_entry(sid, broker)
    settings['MODE'] = 'paper'
    assert ex.request_exit(sid, broker=broker)['status'] == 'closed'
    assert len(broker.orders) == 2


def test_terminal_history_waits_for_execution_details(env):
    sid, broker, _, messages = env
    broker.initial_status = 'OPEN'
    ex.submit_entry(sid, broker)
    broker.snapshots['1'] = OrderExecution('1', 'COMPLETE', 10, (), 'BUY', 'TEST-PE', 10)
    row = ex.reconcile_trade(sid, broker)
    assert row['status'] == 'reconciliation_required'
    assert db.execution_orders(sid)[0]['status'] == 'RECONCILING'
    broker.set_snapshot('1', 'BUY', 'COMPLETE', [(10, 100)])
    assert ex.reconcile_trade(sid, broker)['status'] == 'approved'


def test_paper_close_remains_simulated(env):
    sid, broker, settings, _ = env
    settings['MODE'] = 'paper'
    ex.submit_entry(sid, broker)
    row = ex.request_exit(sid, index_price=90)
    assert row['status'] == 'closed'
    assert row['options_gross_pnl_rs'] is None
    assert broker.orders == []


def test_price_movement_during_option_lookup_blocks_buy(env):
    sid, broker, _, _ = env
    original = broker.place_options_order
    def delayed_lookup(symbol, side, quantity, *, before_submit=None):
        broker.quote_price = 81  # Target nearly reached since the earlier check.
        return original(symbol, side, quantity, before_submit=before_submit)
    broker.place_options_order = delayed_lookup
    row = ex.submit_entry(sid, broker)
    assert row['execution_state'] == 'VALIDATION_REJECTED'
    assert broker.orders == []
    assert row['options_entry_quantity'] is None


def test_reject_button_cannot_hide_open_position(env):
    sid, broker, _, _ = env
    ex.submit_entry(sid, broker)
    db.reject_signal(sid)
    assert db.get_signal(sid)['status'] == 'approved'


def test_ambiguous_entry_blocks_second_entry_and_can_recover(env, monkeypatch):
    sid, broker, _, _ = env
    broker.fail_submit = True
    assert ex.submit_entry(sid, broker)['status'] == 'reconciliation_required'
    with pytest.raises(ValueError):
        ex.submit_entry(sid, broker)
    assert len(broker.orders) == 1
    broker.set_snapshot('confirmed', 'BUY', 'COMPLETE', [(10, 100)])
    monkeypatch.setattr(ex, '_broker', lambda trade, supplied=None: broker)
    order = db.execution_orders(sid)[0]
    ex.bind_broker_order(order['id'], 'confirmed')
    assert ex.reconcile_trade(sid, broker)['status'] == 'approved'


def test_broker_quantity_mismatch_never_closes(env):
    sid, broker, _, _ = env
    ex.submit_entry(sid, broker)
    broker.initial_status = 'OPEN'
    ex.request_exit(sid, broker=broker)
    broker.set_snapshot('2', 'SELL', 'COMPLETE', [(20, 110)], quantity=20)
    row = ex.reconcile_trade(sid, broker)
    assert row['status'] != 'closed'
    assert row['options_exit_quantity'] == 0


def test_delayed_limit_fill_is_captured_during_bounded_polling(env, monkeypatch):
    sid, broker, _, _ = env
    broker.initial_status = 'OPEN'
    def fill_after_first_poll():
        broker.set_snapshot('1', 'BUY', 'COMPLETE', [(10, 100)])
    monkeypatch.setattr(ex, '_poll_wait', fill_after_first_poll)
    row = ex.submit_entry(sid, broker)
    assert row['status'] == 'approved'
    assert row['option_fill_time']
    assert row['underlying_at_option_fill'] == 100
    assert len(broker.orders) == 1
