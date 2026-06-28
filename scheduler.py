"""
Scheduler — runs the scanning loop during market hours.

Timeline:
  10:05 AM  → full zone scan across all 3 timeframes
  Every 5m  → re-scan all TFs for new signals
  15:30     → export daily CSV and shut down

Multi-timeframe confluence flow:
  1. Fetch candles + detect zones for ALL 3 TFs
  2. For each entry zone, check if higher TF zones overlap (same class, same price band)
  3. Log confluence_count and confluence_tfs alongside every signal
"""

import logging
import time
from datetime import date, datetime, timedelta

import schedule

import config
from brokers import get_broker
from engine.confluence import check_confluence
from engine.zones import detect_zones, update_zone_state
from engine.signals import generate_signal
from engine.position_size import calculate as size_trade
from journal.db import init_db, log_signal, trades_today, daily_pnl, get_open_trades, close_trade, zone_signaled_today, expire_old_pending
from journal.export import export_day
import notify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

broker = get_broker()

# TFs in ascending order — lower index = lower timeframe
_TF_ORDER = [config.TF_LOWER, config.TF_INTERMEDIATE, config.TF_HIGHER]


def get_last_trading_day() -> date:
    """Return today if it's a weekday, otherwise roll back to last Friday."""
    today = date.today()
    # weekday(): Mon=0 … Sun=6
    if today.weekday() == 5:      # Saturday → Friday
        return today - timedelta(days=1)
    if today.weekday() == 6:      # Sunday → Friday
        return today - timedelta(days=2)
    return today


def is_market_open() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:        # Saturday or Sunday
        return False
    t = now.strftime("%H:%M")
    return config.MARKET_OPEN <= t <= config.MARKET_CLOSE


def _within_market_hours() -> bool:
    return is_market_open()


def scan():
    if not is_market_open():
        logger.info("Outside market hours — skipping scan.")
        return
    _scan_core()


def scan_now():
    """Run one scan immediately — bypasses market hours check. For testing."""
    init_db()
    if not broker.is_connected():
        logger.error("Broker not connected. Run token refresh first.")
        return
    if not is_market_open():
        logger.warning(
            "Market is CLOSED (last trading day: %s). "
            "Scanning with recent historical data — results are for testing only.",
            get_last_trading_day(),
        )
    else:
        logger.info("── TEST SCAN ──")
    _scan_core()


def _scan_core():
    trade_date = get_last_trading_day().isoformat()  # last trading day when closed, today when open

    if trades_today() >= config.MAX_TRADES_PER_DAY:
        logger.info("Max trades reached for today (%d).", config.MAX_TRADES_PER_DAY)
        return

    if daily_pnl() <= -config.MAX_DAILY_LOSS:
        logger.warning("Daily loss limit hit. No more trades today.")
        return

    logger.info("Scanning %s ...", config.NIFTY_SYMBOL)
    ltp = broker.get_ltp(config.NIFTY_SYMBOL)
    logger.info("LTP: %.2f", ltp)

    # Active filters (re-read each scan so UI changes apply immediately)
    _s                  = config.load_settings()
    active_tfs          = _s.get("SCAN_TIMEFRAMES",      _TF_ORDER)
    active_classes      = set(_s.get("SCAN_ZONE_CLASSES",  ["demand", "supply"]))
    min_confluence      = _s.get("MIN_CONFLUENCE",         config.MIN_CONFLUENCE)
    zone_approach_pts   = _s.get("ZONE_APPROACH_POINTS",   config.ZONE_APPROACH_POINTS)
    disabled_zone_types = set(_s.get("DISABLED_ZONE_TYPES", []))

    # ── Step 1: collect valid zones for every TF ──────────────────────────
    valid_zones: dict[str, list] = {}
    recent_candles: dict[str, list] = {}

    for tf in _TF_ORDER:
        candles = broker.get_historical(config.NIFTY_SYMBOL, tf, days=5)
        if len(candles) < 3:
            logger.warning("Not enough candles on %s", tf)
            valid_zones[tf] = []
            continue

        recent_candles[tf] = candles
        zones = detect_zones(candles[:-1], tf)
        live_slice = candles[-20:]

        good = []
        for z in zones:
            update_zone_state(z, live_slice)
            if z.is_valid:
                good.append(z)

        valid_zones[tf] = good
        logger.info("[%s] %d valid zone(s) found", tf, len(good))

    # ── Step 2: generate signals ──────────────────────────────────────────
    # Only the selected entry TF generates signals.
    # All other TFs are used for confluence scoring only.
    entry_tf = config.load_settings().get("ENTRY_TIMEFRAME", config.TF_LOWER)
    for i, tf in enumerate(_TF_ORDER):
        if tf not in active_tfs:
            continue
        if tf != entry_tf:                 # non-entry TFs → confluence only
            continue
        zones = valid_zones.get(tf, [])
        candles = recent_candles.get(tf, [])
        if not zones or not candles:
            continue

        higher_tf_zones = {
            htf: valid_zones.get(htf, [])
            for htf in _TF_ORDER[i + 1:]
        }

        for zone in zones:
            if zone.zone_class not in active_classes:
                continue

            # Skip if this exact zone already signaled today
            if zone_signaled_today(zone.zone_class, zone.zone_type, tf, zone.proximal):
                continue

            # Skip auto-disabled zone types (self-learning)
            if zone.zone_type in disabled_zone_types:
                logger.debug("[%s] Skipped — %s auto-disabled by learning engine", tf, zone.zone_type)
                continue

            # ── Filter 1: Price proximity ─────────────────────────────────
            dist = abs(ltp - zone.proximal)
            if dist > zone_approach_pts:
                logger.debug(
                    "[%s] Skipped — LTP %.2f is %.0f pts from proximal (max %d)",
                    tf, ltp, dist, zone_approach_pts,
                )
                continue

            # ── Filter 2: Zone validity (no close beyond distal last 3 bars)
            recent_3 = candles[-4:-1]
            if zone.zone_class == "demand":
                if any(c.close < zone.distal for c in recent_3):
                    logger.info("[%s] Skipped — demand zone violated (close < distal %.2f)", tf, zone.distal)
                    continue
            else:
                if any(c.close > zone.distal for c in recent_3):
                    logger.info("[%s] Skipped — supply zone violated (close > distal %.2f)", tf, zone.distal)
                    continue

            # ── Filter 3: 60min trend alignment ──────────────────────────
            candles_60 = recent_candles.get(config.TF_HIGHER, [])
            if len(candles_60) >= 6 and entry_tf != config.TF_HIGHER:
                trend_now  = candles_60[-2].close   # last complete 60min candle
                trend_prev = candles_60[-6].close   # 4 bars earlier
                if trend_now > trend_prev * 1.002:
                    trend_60 = "up"
                elif trend_now < trend_prev * 0.998:
                    trend_60 = "down"
                else:
                    trend_60 = "neutral"
                if zone.zone_class == "demand" and trend_60 == "down":
                    logger.info("[%s] Skipped — demand zone but 60min trend is DOWN", tf)
                    continue
                if zone.zone_class == "supply" and trend_60 == "up":
                    logger.info("[%s] Skipped — supply zone but 60min trend is UP", tf)
                    continue

            sizing = size_trade(zone.proximal, zone.distal, trades_today())
            if sizing.get("error"):
                continue

            confluence = check_confluence(zone, higher_tf_zones)

            signal = generate_signal(
                zone=zone,
                ltp=ltp,
                prev_candles=candles[-10:],
                confluence=confluence,
            )
            if signal is None:
                continue
            if signal.confluence.count < min_confluence:
                logger.info(
                    "[%s] Skipped — confluence %d < min %d required",
                    tf, signal.confluence.count, min_confluence,
                )
                continue

            data = {**signal.as_dict(), "position_size": sizing["position_size"], "date": trade_date}
            sig_id = log_signal(data)
            logger.info(
                "[%s] SIGNAL #%d | %s %s | Score %.1f | Confluence %d TF (%s) | "
                "Entry %.2f | SL %.2f | TGT %.2f",
                tf, sig_id,
                signal.zone.zone_class.upper(), signal.zone.zone_type,
                signal.boosters.total,
                signal.confluence.count,
                signal.confluence.label(),
                signal.entry, signal.stop_loss, signal.intraday_target,
            )
            notify.signal_detected(
                signal_id=sig_id,
                zone_class=signal.zone.zone_class,
                zone_type=signal.zone.zone_type,
                timeframe=tf,
                entry=signal.entry,
                sl=signal.stop_loss,
                target=signal.intraday_target,
                score=signal.boosters.total,
                confluence=signal.confluence.label(),
            )


def monitor_open_trades():
    """Check all approved open trades against current LTP. Auto-exit on target or SL hit."""
    open_trades = get_open_trades()
    if not open_trades:
        return

    try:
        ltp = broker.get_ltp(config.NIFTY_SYMBOL)
    except Exception as e:
        logger.warning("monitor_open_trades: could not fetch LTP — %s", e)
        return

    for row in open_trades:
        t = dict(row)
        tid        = t["id"]
        zone_class = t["zone_class"]
        stop_loss  = t["stop_loss"]
        target     = t["intraday_target"]

        closed = False
        if zone_class == "demand":          # expecting price to rise
            if ltp >= target:
                pnl = round(target - t["entry"], 2)
                close_trade(tid, target, "target")
                logger.info("AUTO-EXIT #%d TARGET hit at %.2f (LTP %.2f)", tid, target, ltp)
                notify.trade_closed(tid, target, "target", pnl)
                closed = True
            elif ltp <= stop_loss:
                pnl = round(stop_loss - t["entry"], 2)
                close_trade(tid, stop_loss, "stoploss")
                logger.info("AUTO-EXIT #%d STOPLOSS hit at %.2f (LTP %.2f)", tid, stop_loss, ltp)
                notify.trade_closed(tid, stop_loss, "stoploss", pnl)
                closed = True
        else:                               # supply — expecting price to fall
            if ltp <= target:
                pnl = round(t["entry"] - target, 2)
                close_trade(tid, target, "target")
                logger.info("AUTO-EXIT #%d TARGET hit at %.2f (LTP %.2f)", tid, target, ltp)
                notify.trade_closed(tid, target, "target", pnl)
                closed = True
            elif ltp >= stop_loss:
                pnl = round(t["entry"] - stop_loss, 2)
                close_trade(tid, stop_loss, "stoploss")
                logger.info("AUTO-EXIT #%d STOPLOSS hit at %.2f (LTP %.2f)", tid, stop_loss, ltp)
                notify.trade_closed(tid, stop_loss, "stoploss", pnl)
                closed = True

        if closed:
            try:
                import autolearn
                autolearn.check_and_learn()
            except Exception as e:
                logger.debug("autolearn error: %s", e)


def check_pending_freshness():
    """Filter 4: auto-expire pending signals where price has already touched the proximal.
    If price enters the zone while signal is still waiting for approval, the entry is missed."""
    from journal.db import get_pending_signals, expire_signal as _expire
    pending = get_pending_signals()
    if not pending:
        return
    try:
        ltp = broker.get_ltp(config.NIFTY_SYMBOL)
    except Exception as e:
        logger.debug("check_pending_freshness: LTP fetch failed — %s", e)
        return

    for row in pending:
        t          = dict(row)
        proximal   = t["proximal"]
        zone_class = t["zone_class"]
        sig_id     = t["id"]
        touched = (
            (zone_class == "demand" and ltp <= proximal) or
            (zone_class == "supply" and ltp >= proximal)
        )
        if touched:
            _expire(sig_id, f"touched while pending — LTP {ltp:.2f} crossed proximal {proximal:.2f}")
            logger.info(
                "Auto-expired pending #%d — LTP %.2f touched proximal %.2f while awaiting approval",
                sig_id, ltp, proximal,
            )


def end_of_day():
    """Close any still-open trades at EOD price, then export the day's CSV."""
    open_trades = get_open_trades()
    if open_trades:
        try:
            ltp = broker.get_ltp(config.NIFTY_SYMBOL)
        except Exception:
            ltp = None
        for row in open_trades:
            t = dict(row)
            exit_price = ltp or t["entry"]   # fallback to entry if LTP unavailable
            close_trade(t["id"], exit_price, "eod")
            logger.info("EOD close #%d at %.2f", t["id"], exit_price)

    path = export_day()
    logger.info("End of day export → %s", path)

    from journal.db import get_signals_for_date
    from datetime import date as _date
    today = _date.today().isoformat()
    closed = [dict(r) for r in get_signals_for_date(today) if r["result"] is not None]
    wins   = sum(1 for t in closed if t["result"] == "win")
    losses = sum(1 for t in closed if t["result"] == "loss")
    pnl    = daily_pnl()
    logger.info("Daily P&L: %.2f pts", pnl)
    notify.eod_summary(trades=len(closed), wins=wins, losses=losses, total_pnl=pnl)


def _backup_job():
    try:
        import backup
        backup.run_backup()
    except Exception as e:
        logger.warning("Backup job error: %s", e)


def run():
    init_db()
    logger.info("Trading engine starting | mode=%s | broker=%s", config.MODE, config.BROKER)

    if not broker.is_connected():
        logger.error("Broker not connected. Run token refresh first.")
        return

    import telegram_handler
    telegram_handler.start_polling()

    schedule.every().day.at(config.SCAN_START).do(scan)
    schedule.every(5).minutes.do(scan)
    schedule.every(1).minutes.do(monitor_open_trades)
    schedule.every(1).minutes.do(check_pending_freshness)
    schedule.every().day.at("15:20").do(end_of_day)    # 10 min before close
    schedule.every().day.at("15:45").do(_backup_job)   # after EOD close

    logger.info("Scheduler running. Waiting for %s...", config.SCAN_START)

    _last_ltp        = None
    _flat_ticks      = 0          # consecutive 30s ticks with unchanged LTP
    _HOLIDAY_TICKS   = 30         # 30 × 30s = 15 min of no movement → holiday

    while True:
        schedule.run_pending()

        now = datetime.now()
        hhmm = now.strftime("%H:%M")

        # ── Expire stale pending signals ──────────────────────────────────
        expiry_min = config.load_settings().get("SIGNAL_EXPIRY_MINUTES", config.SIGNAL_EXPIRY_MINUTES)
        expire_old_pending(expiry_min)

        # ── Graceful stop via UI flag ─────────────────────────────────────
        if config.load_settings().get("engine_state") == "stopped":
            logger.info("Stop flag set — engine shutting down gracefully.")
            config.ENGINE_PID_FILE.unlink(missing_ok=True)
            break

        # ── Auto-stop after market close ──────────────────────────────────
        if now.weekday() < 5 and hhmm >= "15:35":
            logger.info("Market closed (15:35) — engine shutting down.")
            end_of_day()
            config.ENGINE_PID_FILE.unlink(missing_ok=True)
            break

        # ── Holiday detection: no LTP movement for 15 min ─────────────────
        if is_market_open():
            try:
                ltp = broker.get_ltp(config.NIFTY_SYMBOL)
                if ltp == _last_ltp:
                    _flat_ticks += 1
                else:
                    _flat_ticks = 0
                    _last_ltp = ltp
                if _flat_ticks >= _HOLIDAY_TICKS:
                    logger.warning(
                        "LTP unchanged for 15 min during market hours — "
                        "possible holiday. Engine shutting down."
                    )
                    config.ENGINE_PID_FILE.unlink(missing_ok=True)
                    break
            except Exception:
                pass   # network blip — don't stop, just skip this tick

        time.sleep(30)
