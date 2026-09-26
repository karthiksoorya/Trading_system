import ast
import csv
import importlib
from pathlib import Path
from unittest.mock import Mock

from tests.test_execution_lifecycle import env
from engine import execution as ex
from journal import db
from journal.export import export_day
import notify


def test_export_and_notifications_distinguish_estimated_and_actual(env):
    sid, broker, _, messages = env
    ex.submit_entry(sid, broker)
    ex.request_exit(sid, broker=broker)
    row = dict(db.get_signal(sid))
    assert 'Estimated net' in messages[0]
    assert 'broker exit confirmed' in messages[0]
    with export_day(row['date']).open(encoding='utf-8', newline='') as f:
        exported = next(csv.DictReader(f))
    assert float(exported['options_gross_pnl_rs']) == 100
    assert exported['options_net_pnl_rs'] == ''
    assert exported['underlying_fill_basis'] == 'confirmation_quote'
    for order in db.execution_orders(sid):
        db.record_actual_charges(order['id'], 20, 'contract_note', str(order['id']))
    assert 'Actual net: ₹+60.00' in notify.accounting_text(dict(db.get_signal(sid)))


def test_ui_entry_and_exit_paths_cannot_bypass_execution_service():
    root = Path(__file__).resolve().parents[1]
    for filename in ('scheduler.py', 'telegram_handler.py', 'app.py'):
        tree = ast.parse((root / filename).read_text(encoding='utf-8'))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        names = [n.func.id if isinstance(n.func, ast.Name) else n.func.attr if isinstance(n.func, ast.Attribute) else '' for n in calls]
        assert 'place_options_order' not in names
        assert 'close_trade' not in names
        assert 'submit_entry' in names
        assert 'request_exit' in names


def test_telegram_pending_close_is_not_reported_closed(env, monkeypatch):
    sid, broker, _, messages = env
    ex.submit_entry(sid, broker)
    broker.initial_status = 'OPEN'
    monkeypatch.setattr(ex, '_broker', lambda trade, supplied=None: broker)
    import telegram_handler
    post = Mock()
    monkeypatch.setattr(telegram_handler.requests, 'post', post)
    telegram_handler._handle_callback({'id': 'callback', 'data': f'close_{sid}'}, 'test')
    assert db.get_signal(sid)['status'] == 'exit_pending'
    assert 'exit_pending' in post.call_args.kwargs['json']['text']
    assert not messages


def test_scheduler_eod_preserves_unconfirmed_exit(env, monkeypatch):
    sid, broker, _, _ = env
    ex.submit_entry(sid, broker)
    broker.initial_status = 'OPEN'
    broker.get_ltp = lambda symbol: 90
    monkeypatch.setattr(ex, '_broker', lambda trade, supplied=None: broker)
    monkeypatch.setattr('brokers.get_broker', lambda: broker)
    scheduler = importlib.import_module('scheduler')
    monkeypatch.setattr(scheduler, 'broker', broker)
    scheduler.end_of_day()
    assert db.get_signal(sid)['status'] == 'exit_pending'
    assert len(broker.orders) == 2
    scheduler.end_of_day()
    assert len(broker.orders) == 2


def test_scheduler_services_fast_reconciliation_without_live_io(env, monkeypatch, tmp_path):
    _, broker, settings, _ = env
    monkeypatch.setattr('brokers.get_broker', lambda: broker)
    scheduler = importlib.import_module('scheduler')
    monkeypatch.setattr(scheduler, 'broker', broker)
    monkeypatch.setattr(scheduler, 'is_market_open', lambda: False)
    monkeypatch.setattr(scheduler.config, 'ENGINE_PID_FILE', tmp_path / 'engine.pid')
    from datetime import datetime
    class Morning:
        @staticmethod
        def now():
            return datetime(2026, 9, 28, 10, 0)
    monkeypatch.setattr(scheduler, 'datetime', Morning)
    monkeypatch.setattr('telegram_handler.start_polling', lambda: None)
    schedule = Mock()
    monkeypatch.setattr(scheduler, 'schedule', schedule)
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds)
        settings['engine_state'] = 'stopped'
    monkeypatch.setattr(scheduler.time, 'sleep', sleep)
    scheduler.run()
    assert sleeps == [2]
    schedule.every.assert_any_call(2)
