from datetime import datetime, timedelta
import pytest
from brokers.base import IST, UnderlyingObservation
from engine.entry_validation import validate_entry


@pytest.mark.parametrize('direction', ['demand', 'supply'])
def test_reward_and_risk_boundaries(direction):
    now = datetime(2026, 9, 26, 11, 0, tzinfo=IST)
    sign = 1 if direction == 'demand' else -1
    trade = dict(date='2026-09-26', time_signal='10:59:00', zone_class=direction,
                 entry=100, stop_loss=100-sign*10, intraday_target=100+sign*20)
    settings = dict(MIN_REMAINING_RR=1, MIN_REMAINING_REWARD_FRACTION=.5)
    def quote(price, stamp=now):
        return UnderlyingObservation(price, stamp.isoformat())
    assert validate_entry(trade, quote(100+sign*5), settings, now)['remaining_rr'] == 1
    with pytest.raises(ValueError, match='reward/risk'):
        validate_entry(trade, quote(100+sign*5.01), settings, now)
    settings['MIN_REMAINING_RR'] = 0
    assert validate_entry(trade, quote(100+sign*10), settings, now)
    with pytest.raises(ValueError, match='Late entry'):
        validate_entry(trade, quote(100+sign*10.01), settings, now)
    for price in (100+sign*20, 100-sign*10, float('nan')):
        with pytest.raises(ValueError):
            validate_entry(trade, quote(price), settings, now)
    with pytest.raises(ValueError, match='Stale'):
        validate_entry(trade, quote(100, now-timedelta(seconds=6)), settings, now)
    trade['time_signal'] = '09:00:00'
    with pytest.raises(ValueError, match='expired'):
        validate_entry(trade, quote(100), settings, now)


def test_missing_quote_timestamp_is_rejected():
    with pytest.raises(ValueError, match='quote'):
        validate_entry({}, UnderlyingObservation(100, None))


@pytest.mark.parametrize('key', ['MIN_REMAINING_RR', 'MIN_REMAINING_REWARD_FRACTION', 'ENTRY_QUOTE_MAX_AGE_SECONDS'])
def test_invalid_safety_settings_are_rejected(key):
    with pytest.raises(ValueError, match='setting'):
        validate_entry({}, UnderlyingObservation(100, None), {key: float('nan')})


def test_live_settings_override_initial_safety_defaults(monkeypatch):
    import config
    now = datetime(2026, 9, 26, 11, 0, tzinfo=IST)
    trade = dict(date='2026-09-26', time_signal='10:59:00', zone_class='demand',
                 entry=100, stop_loss=90, intraday_target=120)
    quote = UnderlyingObservation(111, (now-timedelta(seconds=6)).isoformat())
    settings = dict(MIN_REMAINING_RR=.4, MIN_REMAINING_REWARD_FRACTION=.4, ENTRY_QUOTE_MAX_AGE_SECONDS=7)
    monkeypatch.setattr(config, 'load_settings', lambda: settings)
    assert validate_entry(trade, quote, now=now)['remaining_reward'] == 9
    settings['ENTRY_QUOTE_MAX_AGE_SECONDS'] = 5
    with pytest.raises(ValueError, match='Stale'):
        validate_entry(trade, quote, now=now)
