import config
from brokers.base import BrokerBase


def get_broker() -> BrokerBase:
    if config.BROKER == "kite":
        from brokers.kite_adapter import KiteAdapter
        return KiteAdapter()
    if config.BROKER == "upstox":
        from brokers.upstox_adapter import UpstoxAdapter
        return UpstoxAdapter()
    raise ValueError(f"Unknown broker '{config.BROKER}'. Set BROKER in config.py.")
