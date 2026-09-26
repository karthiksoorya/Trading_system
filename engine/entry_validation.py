"""Execution-time checks only. Zone discovery and targets are unchanged."""
import math
from datetime import datetime

import config
from brokers.base import IST, broker_time


def validate_entry(trade, quote, settings=None, now=None):
    settings = config.load_settings() if settings is None else settings
    now = now or datetime.now(IST)
    def setting(name):
        value = float(settings.get(name, getattr(config, name)))
        if not math.isfinite(value) or value < 0:
            raise ValueError(f'Invalid execution setting: {name}')
        return value
    # Validate every threshold even if a preceding branch would not use it.
    for name in ('MIN_REMAINING_RR', 'MIN_REMAINING_REWARD_FRACTION',
                 'ENTRY_QUOTE_MAX_AGE_SECONDS', 'SIGNAL_EXPIRY_MINUTES'):
        setting(name)
    stamp = broker_time(quote.observed_at)
    if stamp is None or not math.isfinite(quote.price) or quote.price <= 0:
        raise ValueError("Missing or invalid NIFTY quote")
    age = (now - datetime.fromisoformat(stamp)).total_seconds()
    if age < -1 or age > setting("ENTRY_QUOTE_MAX_AGE_SECONDS"):
        raise ValueError("Stale NIFTY quote")
    signal_time = broker_time(f"{trade['date']}T{trade['time_signal']}")
    if signal_time is None or not 0 <= (now - datetime.fromisoformat(signal_time)).total_seconds() <= setting("SIGNAL_EXPIRY_MINUTES") * 60:
        raise ValueError("Signal expired or timestamp invalid")
    if trade['zone_class'] not in ('demand', 'supply'):
        raise ValueError('Invalid zone direction')
    direction = 1 if trade['zone_class'] == 'demand' else -1
    entry, stop, target = (float(trade[k]) for k in ('entry', 'stop_loss', 'intraday_target'))
    if not all(math.isfinite(v) for v in (entry, stop, target)):
        raise ValueError("Invalid signal prices")
    risk = direction * (quote.price - stop)
    reward = direction * (target - quote.price)
    planned = direction * (target - entry)
    tolerance = min(abs(entry - stop) * 0.5, 20)
    if direction * (quote.price - entry) < -tolerance:
        raise ValueError("Entry zone may be broken")
    if risk <= 0 or reward <= 0 or planned <= 0:
        raise ValueError("Target passed or stop breached")
    if reward / risk < setting("MIN_REMAINING_RR"):
        raise ValueError("Insufficient remaining reward/risk")
    if reward / planned < setting("MIN_REMAINING_REWARD_FRACTION"):
        raise ValueError("Late entry: insufficient planned reward remaining")
    return {"remaining_reward": reward, "remaining_risk": risk,
            "remaining_rr": reward / risk, "remaining_reward_fraction": reward / planned}
