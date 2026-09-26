from unittest.mock import Mock
import pytest
from brokers.base import normalize_execution, broker_time
from brokers.kite_adapter import KiteAdapter
from brokers.upstox_adapter import UpstoxAdapter


def raw_order(status='COMPLETE', upstox=False):
    return dict(order_id='123', status=status, filled_quantity=10, quantity=10,
                transaction_type='BUY', tradingsymbol='TEST-PE', instrument_token='NSE_FO|123' if upstox else 123)


def raw_fills():
    return [dict(order_id='123', trade_id='t1', quantity=4, average_price=100,
                 exchange_timestamp='26-Sep-2026 10:01:02'),
            dict(order_id='123', trade_id='t2', quantity=6, average_price=110,
                 exchange_timestamp='2026-09-26 10:01:03')]


def test_kite_adapter_gets_trades_not_submission_timestamp():
    adapter = KiteAdapter.__new__(KiteAdapter)
    adapter._kite = Mock()
    adapter._kite.order_history.return_value = [raw_order('OPEN'), raw_order()]
    adapter._kite.order_trades.return_value = raw_fills()
    result = adapter.get_order_execution('123')
    assert result.status == 'COMPLETE'
    assert result.filled_quantity == 10
    assert result.fills[0].timestamp == '2026-09-26T10:01:02+05:30'
    assert result.symbol == 'TEST-PE'
    assert sum(f.price * f.quantity for f in result.fills) == 1060


def test_upstox_adapter_normalizes_trade_timestamp(monkeypatch):
    adapter = UpstoxAdapter.__new__(UpstoxAdapter)
    adapter._access_token = 'test'
    def get(url, **kwargs):
        reply = Mock()
        reply.json.return_value = {'data': raw_fills() if url.endswith('/trades') else raw_order('complete', True)}
        return reply
    monkeypatch.setattr('brokers.upstox_adapter.requests.get', get)
    result = adapter.get_order_execution('123')
    assert result.symbol == 'NSE_FO|123'
    assert result.terminal
    assert result.fills[0].timestamp.endswith('+05:30')


@pytest.mark.parametrize('status', ['OPEN', 'CANCELLED', 'REJECTED'])
def test_nonfilled_order_status_is_preserved(status):
    raw = raw_order(status)
    raw['filled_quantity'] = 0
    result = normalize_execution('123', raw, [])
    assert result.status == status
    assert result.fills == ()


def test_missing_timestamp_stays_unknown_and_wrong_order_is_rejected():
    fills = raw_fills()
    fills[0]['exchange_timestamp'] = None
    result = normalize_execution('123', raw_order(), fills)
    assert result.fills[0].timestamp is None
    assert result.fills[0].timestamp_basis == 'unknown'
    fills[0]['order_id'] = 'other'
    with pytest.raises(ValueError):
        normalize_execution('123', raw_order(), fills)
    assert broker_time('bad timestamp') is None


def test_kite_quote_uses_broker_quote_timestamp():
    adapter = KiteAdapter.__new__(KiteAdapter)
    adapter._kite = Mock()
    adapter._kite.quote.return_value = {'NIFTY': {'last_price': 24000, 'timestamp': '2026-09-26 10:00:00'}}
    observation = adapter.get_underlying_observation('NIFTY')
    assert observation.price == 24000
    assert observation.observed_at == '2026-09-26T10:00:00+05:30'
    assert observation.basis == 'confirmation_quote'


@pytest.mark.parametrize('adapter_type', ['kite', 'upstox'])
def test_last_moment_validation_precedes_order_post(adapter_type, monkeypatch):
    from brokers.base import EntryValidationRejected
    if adapter_type == 'kite':
        adapter = KiteAdapter.__new__(KiteAdapter)
        adapter._kite = Mock()
        adapter._kite.ltp.return_value = {'NFO:TEST': {'last_price': 100}}
        submit = adapter._kite.place_order
    else:
        adapter = UpstoxAdapter.__new__(UpstoxAdapter)
        adapter.get_options_ltp = lambda symbol: 100
        adapter._access_token = 'test'
        submit = Mock()
        monkeypatch.setattr('brokers.upstox_adapter.requests.post', submit)
    def reject():
        raise EntryValidationRejected('price moved')
    with pytest.raises(EntryValidationRejected):
        adapter.place_options_order('TEST', 'BUY', 10, before_submit=reject)
    submit.assert_not_called()
