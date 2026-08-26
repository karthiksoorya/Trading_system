import config
from brokers.base import BrokerBase


def get_broker() -> BrokerBase:
    # Read from settings.json at call time so a UI change + engine restart picks it up.
    broker = config.load_settings().get("BROKER", config.BROKER)
    if broker == "kite":
        from brokers.kite_adapter import KiteAdapter
        return KiteAdapter()
    if broker == "upstox":
        from brokers.upstox_adapter import UpstoxAdapter
        return UpstoxAdapter()
    raise ValueError(f"Unknown broker '{broker}'. Choose 'kite' or 'upstox' in Settings.")
