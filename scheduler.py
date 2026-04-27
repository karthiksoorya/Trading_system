"""
Scheduler — runs the scanning loop during market hours.

Timeline:
  10:05 AM  → full zone scan across all 3 timeframes
  Every 5m  → re-scan lower TF for new signals
  15:30     → export daily CSV and shut down
"""

import logging
import time
from datetime import datetime

import schedule

import config
from brokers import get_broker
from engine.candle import is_boring, is_exciting
from engine.zones import detect_zones, update_zone_state
from engine.signals import generate_signal
from engine.position_size import calculate as size_trade
from journal.db import init_db, log_signal, trades_today, daily_pnl
from journal.export import export_day

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

broker = get_broker()


def _within_market_hours() -> bool:
    now = datetime.now().strftime("%H:%M")
    return config.MARKET_OPEN <= now <= config.MARKET_CLOSE


def scan():
    if not _within_market_hours():
        logger.info("Outside market hours — skipping scan.")
        return
    _scan_core()


def scan_now():
    """Run a single scan immediately — bypasses market hours check. For testing."""
    init_db()
    if not broker.is_connected():
        logger.error("Broker not connected. Run token refresh first.")
        return
    logger.info("── TEST SCAN (bypassing market hours) ──")
    scan.__wrapped__() if hasattr(scan, "__wrapped__") else _scan_core()


def _scan_core():
    """Inner scan logic shared by scan() and scan_now()."""
    if trades_today() >= config.MAX_TRADES_PER_DAY:
        logger.info("Max trades reached for today (%d).", config.MAX_TRADES_PER_DAY)
        return

    pnl = daily_pnl()
    if pnl <= -config.MAX_DAILY_LOSS:
        logger.warning("Daily loss limit hit (%.2f). No more trades today.", pnl)
        return

    logger.info("Scanning %s ...", config.NIFTY_SYMBOL)
    ltp = broker.get_ltp(config.NIFTY_SYMBOL)
    logger.info("LTP: %.2f", ltp)

    for tf in [config.TF_HIGHER, config.TF_INTERMEDIATE, config.TF_LOWER]:
        candles = broker.get_historical(config.NIFTY_SYMBOL, tf, days=5)
        if len(candles) < 3:
            logger.warning("Not enough candles on %s", tf)
            continue

        zones = detect_zones(candles[:-1], tf)
        live_candles = candles[-20:]

        for zone in zones:
            update_zone_state(zone, live_candles)
            if not zone.is_valid:
                continue

            sizing = size_trade(zone.proximal, zone.distal, trades_today())
            if sizing.get("error"):
                continue

            signal = generate_signal(zone=zone, ltp=ltp, prev_candles=candles[-10:])
            if signal is None:
                continue

            data = {**signal.as_dict(), "position_size": sizing["position_size"]}
            sig_id = log_signal(data)
            logger.info(
                "[%s] SIGNAL #%d | %s %s | Score %.1f | Entry %.2f | SL %.2f | TGT %.2f",
                tf, sig_id,
                signal.zone.zone_class.upper(), signal.zone.zone_type,
                signal.boosters.total,
                signal.entry, signal.stop_loss, signal.intraday_target,
            )


def end_of_day():
    path = export_day()
    logger.info("End of day export complete → %s", path)
    logger.info("Daily P&L: %.2f pts", daily_pnl())


def run():
    init_db()
    logger.info("Trading engine starting | mode=%s | broker=%s", config.MODE, config.BROKER)

    if not broker.is_connected():
        logger.error("Broker not connected. Run token refresh first.")
        return

    # Schedule jobs
    schedule.every().day.at(config.SCAN_START).do(scan)
    schedule.every(5).minutes.do(scan)
    schedule.every().day.at(config.MARKET_CLOSE).do(end_of_day)

    logger.info("Scheduler running. Waiting for %s...", config.SCAN_START)
    while True:
        schedule.run_pending()
        time.sleep(30)
