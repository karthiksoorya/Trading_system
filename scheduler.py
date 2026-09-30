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
import math
import threading
import time
from datetime import date, datetime, timedelta

import schedule

import config

from brokers import get_broker
from engine.confluence import check_confluence
from engine.zones import detect_zones, update_zone_state
from engine.signals import generate_signal
from engine.position_size import calculate as size_trade
from journal.db import (init_db, log_signal, trades_today, daily_pnl, daily_options_pnl,
                        get_open_trades, zone_signaled_today, expire_old_pending,
                        reject_signal, update_signal_sim_outcome, update_signal_agent_verdict)
from journal.export import export_day
import notify
from engine.execution import submit_entry, request_exit, reconcile_open_trades, check_daily_loss

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

broker = get_broker()

# TFs in ascending order — lower index = lower timeframe
_TF_ORDER = [config.TF_LOWER, config.TF_INTERMEDIATE, config.TF_HIGHER]

# Scan concurrency guard — prevents two overlapping scans when one hangs
_scan_lock = threading.Lock()

# VIX direction tracking — tracks today's running low so a temporary spike that
# resolves doesn't permanently block CE signals for the rest of the day.
_vix_baseline: float = 0.0
_vix_baseline_date: str = ""

# IV Rank cache — refreshed once per day (365-day VIX fetch is slow, do it once)
_iv_rank_cache: dict = {"rank": None, "date": ""}


def _get_iv_rank() -> float | None:
    """Compute India VIX IV Rank (0–100) using 52-week high/low. Cached daily.

    IV Rank = (current_vix - 52w_low) / (52w_high - 52w_low) × 100
    Low rank = cheap premium = safer to buy naked options.
    High rank = expensive premium = IV crush risk even on correct direction.
    Returns None if data unavailable (filter is skipped gracefully).
    """
    today_str = date.today().isoformat()
    if _iv_rank_cache["date"] == today_str and _iv_rank_cache["rank"] is not None:
        return _iv_rank_cache["rank"]
    try:
        candles = broker.get_historical(config.VIX_SYMBOL, "day", 365)
        if len(candles) < 20:
            logger.warning("IV Rank: too few VIX candles (%d) — skipping rank filter", len(candles))
            return None
        closes = [c.close for c in candles]
        current = closes[-1]
        hi = max(closes)
        lo = min(closes)
        rank = round((current - lo) / (hi - lo) * 100, 1) if hi != lo else 50.0
        _iv_rank_cache["rank"] = rank
        _iv_rank_cache["date"] = today_str
        logger.info("IV Rank: %.1f%% (VIX %.2f, 52w range %.2f – %.2f)", rank, current, lo, hi)
        return rank
    except Exception as e:
        logger.warning("IV Rank fetch failed — filter skipped: %s", e)
        return None


def _norm_cdf(x: float) -> float:
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def _bs_delta(spot: float, strike: float, days_to_expiry: int, vix: float, option_type: str) -> float:
    """Black-Scholes delta. vix is India VIX value (e.g. 14.2). Returns rounded delta."""
    T = max(days_to_expiry / 365.0, 1 / 365.0)
    sigma = max(vix / 100.0, 0.01)
    r = 0.07   # India 10yr risk-free rate ~7%
    try:
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        return round(_norm_cdf(d1) if option_type == "CE" else _norm_cdf(d1) - 1.0, 2)
    except Exception:
        return 0.5


def _days_to_next_expiry() -> int:
    """Days until next Nifty weekly expiry (Tuesday), skipping if <= 1 day away."""
    today = date.today()
    days_ahead = (1 - today.weekday()) % 7
    if days_ahead <= 1:
        days_ahead += 7
    return days_ahead


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


def _run_agent_eval(signal, zone, vix: float | None) -> dict:
    """Call agent evaluator. Returns REVIEW on any failure — never silently passes signals."""
    try:
        from agents.evaluator import evaluate
        return evaluate(
            signal.as_dict(),
            {"departure_strength": zone.departure_strength, "base_compression": zone.base_compression},
            vix,
        )
    except Exception as e:
        logger.warning("Agent evaluator error — returning REVIEW: %s", e)
        return {"verdict": "REVIEW", "reason": f"evaluator exception: {str(e)[:80]}"}


def _run_shadow_eval(signal, zone, vix: float | None, sig_id: int, live_verdict: str) -> None:
    """
    Evaluate the same signal using the candidate memory (if any exists) and log the result.
    Called for EVERY signal — including ones the live agent SKIP'd — so we accumulate
    enough data to validate the candidate before promoting it.
    The shadow result is logged to agent/shadow_log.jsonl and never affects live decisions.
    """
    import json as _json
    from datetime import datetime as _dt
    from pathlib import Path as _Path

    agent_dir  = _Path(__file__).parent / "agents"
    candidates = sorted(agent_dir.glob("memory_candidate_*.json"), reverse=True)
    if not candidates:
        return

    candidate_path = candidates[0]
    candidate_date = candidate_path.stem.replace("memory_candidate_", "")

    try:
        candidate_mem = _json.loads(candidate_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("Shadow eval: could not load candidate: %s", e)
        return

    try:
        from agents.evaluator import evaluate_with_memory as _eshadow
        shadow = _eshadow(
            signal.as_dict(),
            {"departure_strength": getattr(zone, "departure_strength", None),
             "base_compression":   getattr(zone, "base_compression", None)},
            vix,
            candidate_mem,
        )
    except Exception as e:
        logger.debug("Shadow eval failed: %s", e)
        return

    entry = {
        "ts":             _dt.now().isoformat(timespec="seconds"),
        "date":           _dt.now().strftime("%Y-%m-%d"),
        "candidate_date": candidate_date,
        "signal_id":      sig_id,
        "signal_key":     (f"{signal.zone.zone_class}_{signal.zone.zone_type}"
                           f"_{signal.zone.timeframe}"),
        "live_verdict":   live_verdict,
        "shadow_verdict": shadow["verdict"],
        "shadow_reason":  shadow["reason"],
    }
    try:
        shadow_log = agent_dir / "shadow_log.jsonl"
        with shadow_log.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(entry) + "\n")
        if shadow["verdict"] != live_verdict:
            logger.info("[SHADOW] Signal #%d: live=%s shadow=%s — %s",
                        sig_id, live_verdict, shadow["verdict"], shadow["reason"])
    except Exception as e:
        logger.debug("Shadow log write failed: %s", e)


def scan():
    if not is_market_open():
        logger.info("Outside market hours — skipping scan.")
        return
    if not _scan_lock.acquire(blocking=False):
        logger.warning("Previous scan still running — skipping this cycle.")
        return

    def _run():
        try:
            _scan_core()
        except Exception as e:
            logger.exception("Unhandled exception in scan thread: %s", e)
        finally:
            _scan_lock.release()

    threading.Thread(target=_run, name="scan-worker", daemon=True).start()


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

    # FIX F: use live settings, not stale module-level values
    _s_early = config.load_settings()

    _max_trades = _s_early.get("MAX_TRADES_PER_DAY", config.MAX_TRADES_PER_DAY)
    if trades_today() >= _max_trades:
        logger.info("Max trades reached for today (%d).", _max_trades)
        return

    try:
        check_daily_loss(_s_early)
    except ValueError as exc:
        logger.warning("New entries blocked: %s", exc)
        return

    _daily_options_target = _s_early.get("DAILY_OPTIONS_TARGET", config.DAILY_OPTIONS_TARGET)
    if _daily_options_target > 0:
        _opts_pnl = daily_options_pnl()
        if _opts_pnl is not None and _opts_pnl >= _daily_options_target:
            logger.info(
                "Daily options target hit (₹%.0f ≥ ₹%.0f). Protecting gains — no more trades today.",
                _opts_pnl, _daily_options_target,
            )
            return

    logger.info("Scanning %s ...", config.NIFTY_SYMBOL)
    ltp = broker.get_ltp(config.NIFTY_SYMBOL)
    logger.info("LTP: %.2f", ltp)

    # VIX level + direction filter
    _vix_max = _s_early.get("VIX_MAX", config.VIX_MAX)
    vix = None
    vix_rising = False
    try:
        global _vix_baseline, _vix_baseline_date
        vix = broker.get_ltp(config.VIX_SYMBOL)
        today_str = date.today().isoformat()
        if _vix_baseline_date != today_str or _vix_baseline == 0.0:
            _vix_baseline = vix
            _vix_baseline_date = today_str
            logger.info("India VIX baseline set: %.2f", vix)
        elif vix < _vix_baseline:
            # VIX dropped below today's low — update baseline so a resolved spike
            # doesn't permanently block CE signals for the rest of the day.
            _vix_baseline = vix
            logger.info("India VIX baseline updated (VIX fell): %.2f", _vix_baseline)
        vix_rising = vix > _vix_baseline * 1.05   # >5% above today's running low
        logger.info("India VIX: %.2f (low %.2f, limit %.1f) — %s",
                    vix, _vix_baseline, _vix_max,
                    "RISING ⚠️ CE signals blocked" if vix_rising else "stable")
        if vix > _vix_max:
            logger.warning(
                "India VIX %.2f > %.1f — skipping scan. "
                "High VIX inflates option premiums and increases risk.",
                vix, _vix_max,
            )
            return
    except Exception as _vix_err:
        logger.debug("VIX fetch failed — proceeding without filter: %s", _vix_err)

    # IV Rank — computed once per day, cached. None = data unavailable, filter skipped.
    _iv_rank_max = _s_early.get("IV_RANK_MAX", config.IV_RANK_MAX)
    _iv_rank     = _get_iv_rank()

    # Active filters — read settings ONCE per scan so all decisions use consistent values
    # BUG 13 fix: previously load_settings() was called 3+ times during a single scan,
    # meaning a dashboard change mid-scan could cause different parts to see different settings.
    _s                  = config.load_settings()
    active_tfs          = _s.get("SCAN_TIMEFRAMES",      _TF_ORDER)
    active_classes      = set(_s.get("SCAN_ZONE_CLASSES",  ["demand", "supply"]))
    min_confluence      = _s.get("MIN_CONFLUENCE",         config.MIN_CONFLUENCE)
    min_risk_pts        = _s.get("MIN_RISK_POINTS",        config.MIN_RISK_POINTS)
    zone_approach_pts   = _s.get("ZONE_APPROACH_POINTS",   config.ZONE_APPROACH_POINTS)
    disabled_zone_types = set(_s.get("DISABLED_ZONE_TYPES", []))
    entry_tf            = _s.get("ENTRY_TIMEFRAME",        config.TF_LOWER)
    _auto_first         = _s.get("AUTO_FIRST_TRADE",       False)
    _auto_first_n       = _s.get("AUTO_FIRST_COUNT",       config.AUTO_FIRST_COUNT)

    # ── Scan window filter ────────────────────────────────────────────────
    _win    = _s.get("SCAN_WINDOW", {"start": "09:15", "end": "10:30"})
    _now_hm = datetime.now().strftime("%H:%M")
    if not (_win["start"] <= _now_hm <= _win["end"]):
        logger.info("Outside scan window (%s–%s) — skipping.", _win["start"], _win["end"])
        return

    # If TIME_EXIT_HOUR is set, stop new signals at the same hour.
    # TIME_EXIT_HOUR already closes open trades; this ensures no new ones open after that.
    _time_exit_hr = _s_early.get("TIME_EXIT_HOUR", config.TIME_EXIT_HOUR)
    if _time_exit_hr and datetime.now().hour >= _time_exit_hr:
        logger.info(
            "Past TIME_EXIT_HOUR (%d:00) — no new signals to avoid afternoon theta decay.",
            _time_exit_hr,
        )
        return

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
    # entry_tf already read from _s above (BUG 13 fix)
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

            # VIX direction filter: rising VIX = fear expanding = IV crush risk on CE
            if zone.zone_class == "demand" and vix_rising:
                logger.info(
                    "[%s] Skipped — VIX rising (%.2f vs %.2f open) — IV crush risk on CE",
                    tf, vix if vix else 0, _vix_baseline,
                )
                continue

            # Time-of-day filter: CE signals only after 11:00 AM (morning IV not settled)
            if zone.zone_class == "demand" and datetime.now().hour < 11:
                logger.info("[%s] Skipped — CE before 11:00 AM, morning IV not settled", tf)
                continue

            # IV Rank filter: skip when premium is historically expensive
            # High IV Rank = IV is in top percentile of 52-week range = crush risk
            if _iv_rank is not None and _iv_rank > _iv_rank_max:
                logger.info(
                    "[%s] Skipped — IV Rank %.1f%% > %.0f%% limit. "
                    "Premium historically expensive — IV crush risk on naked options.",
                    tf, _iv_rank, _iv_rank_max,
                )
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

            # FIX D: collect all valid zones of the opposing class from the entry TF
            # so generate_signal can cap the target at the nearest resistance/support
            opposing_class = "supply" if zone.zone_class == "demand" else "demand"
            opposing_zones = [z for z in zones if z.zone_class == opposing_class and z.is_valid]

            signal = generate_signal(
                zone=zone,
                ltp=ltp,
                prev_candles=candles[-10:],
                confluence=confluence,
                opposing_zones=opposing_zones,
            )
            if signal is None:
                continue
            if signal.confluence.count < min_confluence:
                logger.info(
                    "[%s] Skipped — confluence %d < min %d required",
                    tf, signal.confluence.count, min_confluence,
                )
                continue

            # Min-risk floor: a degenerate zone (tiny doji base) gives a 2R target
            # only a few points away that any bar clips instantly — the "win" can't
            # cover costs. Backtest (Aug 29): |entry-SL| >= 15 is the useful floor.
            _risk = abs(signal.entry - signal.stop_loss)
            if min_risk_pts and _risk < min_risk_pts:
                logger.info(
                    "[%s] Skipped — risk %.1f pts < min %d (degenerate zone)",
                    tf, _risk, min_risk_pts,
                )
                continue

            data = {
                **signal.as_dict(),
                "position_size":     sizing["position_size"],
                "date":              trade_date,
                "departure_strength": getattr(zone, "departure_strength", None),
                "base_compression":   getattr(zone, "base_compression", None),
                "vix_at_signal":      vix,
                "iv_rank_at_signal":  _iv_rank,
            }
            sig_id = log_signal(data)

            # ── Agent evaluation ──────────────────────────────────────────
            _agent_enabled = config.load_settings().get("AGENT_EVAL_ENABLED", True)
            if _agent_enabled:
                _agent = _run_agent_eval(signal, zone, vix)
                update_signal_agent_verdict(sig_id, _agent["verdict"], _agent["reason"])
                _run_shadow_eval(signal, zone, vix, sig_id, _agent["verdict"])
                if _agent["verdict"] == "SKIP":
                    logger.info("[%s] Agent SKIP — %s", tf, _agent["reason"])
                    reject_signal(sig_id, f"agent_skip: {_agent['reason'][:120]}")
                    continue
                _agent_note = _agent["reason"] if _agent["verdict"] == "REVIEW" else None
            else:
                _agent_note = None
                logger.info("[%s] Agent eval disabled — passing signal through", tf)

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

            # Options context for Telegram — delta and VIX computed from data already in memory
            _atm = round(signal.entry / 50) * 50
            _opt_type = "CE" if zone.zone_class == "demand" else "PE"
            _strike_display = (_atm - 50) if _opt_type == "CE" else (_atm + 50)
            _dte   = _days_to_next_expiry()
            _delta = _bs_delta(ltp, _strike_display, _dte, vix or 15.0, _opt_type)
            logger.info("Options context: %d %s | delta %.2f | VIX %.1f | DTE %d",
                        _strike_display, _opt_type, _delta, vix or 0, _dte)

            # _s and _auto_first already read at top of scan (BUG 13 fix)
            _fully_auto    = _s.get("FULLY_AUTOMATED", False)
            _no_open_trade = not get_open_trades()
            _do_auto       = _no_open_trade and (
                _fully_auto or (_auto_first and trades_today() < _auto_first_n)
            )

            if _do_auto:
                _auto_label = ("FULLY-AUTO" if _fully_auto
                               else f"AUTO-FIRST {trades_today() + 1}/{_auto_first_n}")
                logger.info("%s: attempting signal #%d", _auto_label, sig_id)
                _auto_ok = False
                try:
                    execution = submit_entry(sig_id, broker=broker, requester="scheduler")
                    _auto_ok = execution["status"] != "pending"
                    notify._send(f"Entry #{sig_id}: {execution['status']} ({execution.get('execution_state') or 'paper'})")
                except Exception as exc:
                    logger.warning("Entry #%s not submitted: %s", sig_id, exc)
                    notify._send(f"Entry #{sig_id} not submitted: {exc}")

                # If auto-trade didn't happen, fall back to normal Telegram approval
                if not _auto_ok:
                    notify.signal_detected(
                        signal_id=sig_id, zone_class=signal.zone.zone_class,
                        zone_type=signal.zone.zone_type, timeframe=tf,
                        entry=signal.entry, sl=signal.stop_loss,
                        target=signal.intraday_target, score=signal.boosters.total,
                        confluence=signal.confluence.label(),
                        strike=_strike_display, opt_type=_opt_type,
                        delta=_delta, vix=vix, iv_rank=_iv_rank,
                        agent_note=_agent_note,
                    )
            else:
                # ── Normal flow: send Telegram notification for manual approval ──
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
                    strike=_strike_display, opt_type=_opt_type,
                    delta=_delta, vix=vix, iv_rank=_iv_rank,
                    agent_note=_agent_note,
                )


def _get_options_ltp(opts_sym: str, trade=None) -> float | None:
    """Fetch current options premium via the active broker. Returns None on failure."""
    if not opts_sym:
        return None
    try:
        from engine.execution import _broker
        adapter = _broker(trade) if trade and trade.get('mode') == 'live' else broker
        return adapter.get_options_ltp(opts_sym)
    except Exception as e:
        logger.debug("Options LTP fetch failed for %s: %s", opts_sym, e)
        return None


def _calc_options_pnl(entry_premium: float, current_premium: float, lot_size: int) -> float:
    """Options P&L in rupees: (exit - entry) × lot_size."""
    return round((current_premium - entry_premium) * lot_size, 2)


def monitor_open_trades():
    """Check all approved open trades against current LTP. Auto-exit on:
    1. Index target hit
    2. Index SL hit
    3. Options premium up >= OPTIONS_TRAIL_PCT (profit lock)
    4. Options premium down >= OPTIONS_SL_PCT (loss cut)
    5. Time exit at TIME_EXIT_HOUR (afternoon theta cutoff)
    """
    reconcile_open_trades()
    open_trades = get_open_trades()
    if not open_trades:
        return

    try:
        ltp = broker.get_ltp(config.NIFTY_SYMBOL)
    except Exception as e:
        logger.warning("monitor_open_trades: could not fetch LTP — %s", e)
        return

    _s            = config.load_settings()
    trail_pct     = _s.get("OPTIONS_TRAIL_PCT", config.OPTIONS_TRAIL_PCT)
    options_sl_pct = _s.get("OPTIONS_SL_PCT",   config.OPTIONS_SL_PCT)
    time_exit_hr  = _s.get("TIME_EXIT_HOUR",    config.TIME_EXIT_HOUR)
    now_hour      = datetime.now().hour

    for row in open_trades:
        t = dict(row)
        if t["mode"] == "live" and (t["status"] not in ("approved", "entry_pending") or not t.get("options_entry_quantity")):
            continue
        tid            = t["id"]
        zone_class     = t["zone_class"]
        entry          = t["entry"]
        stop_loss      = t["stop_loss"]
        target         = t["intraday_target"]
        entry_premium  = t.get("options_entry_price") or 0
        opts_sym       = t.get("options_symbol") or ""
        lot_size       = max(0, (t.get('options_entry_quantity') or 0) - (t.get('options_exit_quantity') or 0))

        # ── Fetch live options premium ────────────────────────────────────
        current_premium = _get_options_ltp(opts_sym, t) if opts_sym else None

        closed = False
        close_reason = None
        close_price  = None

        # ── 1. Index target / SL ─────────────────────────────────────────
        if zone_class == "demand":
            if ltp >= target:
                close_reason, close_price = "target", target
            elif ltp <= stop_loss:
                close_reason, close_price = "stoploss", stop_loss
        else:
            if ltp <= target:
                close_reason, close_price = "target", target
            elif ltp >= stop_loss:
                close_reason, close_price = "stoploss", stop_loss

        # ── 2. Options trailing profit lock ──────────────────────────────
        if not close_reason and trail_pct > 0 and entry_premium > 0 and current_premium:
            gain_pct = (current_premium - entry_premium) / entry_premium * 100
            if gain_pct >= trail_pct:
                opts_pnl = _calc_options_pnl(entry_premium, current_premium, lot_size)
                logger.info(
                    "OPTIONS TRAIL EXIT #%d — premium %.2f→%.2f (+%.1f%%) options P&L ₹%.0f",
                    tid, entry_premium, current_premium, gain_pct, opts_pnl,
                )
                notify.options_trail_exit(tid, entry_premium, current_premium, gain_pct, opts_pnl)
                close_reason, close_price = "options_trail", ltp
                closed = True

        # ── 3. Options SL — cut loss when premium drops too far ──────────
        if not close_reason and options_sl_pct > 0 and entry_premium > 0 and current_premium:
            loss_pct = (entry_premium - current_premium) / entry_premium * 100
            if loss_pct >= options_sl_pct:
                opts_pnl = _calc_options_pnl(entry_premium, current_premium, lot_size)
                logger.info(
                    "OPTIONS SL EXIT #%d — premium %.2f→%.2f (−%.1f%%) options P&L ₹%.0f",
                    tid, entry_premium, current_premium, loss_pct, opts_pnl,
                )
                notify.options_sl_exit(tid, entry_premium, current_premium, loss_pct, opts_pnl)
                close_reason, close_price = "options_sl", ltp
                closed = True

        # ── 4. Time exit ─────────────────────────────────────────────────
        if not close_reason and time_exit_hr > 0 and now_hour >= time_exit_hr:
            opts_pnl = _calc_options_pnl(entry_premium, current_premium, lot_size) \
                       if (entry_premium > 0 and current_premium) else None
            logger.info(
                "TIME EXIT #%d at %02d:00 — index %.2f, options P&L %s",
                tid, time_exit_hr, ltp,
                f"₹{opts_pnl:+.0f}" if opts_pnl is not None else "unknown",
            )
            notify.time_exit(tid, time_exit_hr, opts_pnl)
            close_reason, close_price = "time_exit", ltp
            closed = True

        # A trigger requests an exit; only confirmed fills complete a live trade.
        if close_reason:
            try:
                outcome = request_exit(tid, close_reason, requester="system", index_price=close_price or ltp)
                closed = outcome['status'] == 'closed'
                if closed and outcome['mode'] != 'live':
                    notify.trade_closed(tid, outcome['exit_price'], close_reason, outcome['pnl_points'])
                if closed:
                    import autolearn
                    autolearn.check_and_learn()
            except Exception as exc:
                logger.warning("Exit #%s remains unresolved: %s", tid, exc)


def retry_pending_entries():
    """Retry auto-entry for still-pending signals as price approaches the zone.

    validate_entry() (engine/entry_validation.py) only accepts an entry once price is
    favorably close to it — a single attempt at signal-detection time in _scan_core()
    is almost always premature, since price usually hasn't reached the zone yet. This
    runs every minute so auto-trade gets repeated chances as price moves, instead of
    only ever trying once and giving up.

    This replaces the old "auto-expire the instant price touches the zone" behavior
    (removed 2026-09-30): that logic raced directly against both auto-trade and manual
    Telegram/dashboard approval, expiring signals in the exact narrow window
    validate_entry() would otherwise have accepted them — on 2026-09-30 this produced
    zero trades across 6 well-scored signals despite the engine working correctly.
    validate_entry() already rejects genuinely late/broken entries (zero or negative
    remaining reward/risk, zone broken beyond tolerance), making the old check
    redundant now that a precise gate exists at actual submission time.
    SIGNAL_EXPIRY_MINUTES (via expire_old_pending in run()) remains the timeout backstop.
    """
    from journal.db import get_pending_signals
    pending = get_pending_signals()
    if not pending:
        return
    if get_open_trades():
        return
    _s            = config.load_settings()
    _fully_auto   = _s.get("FULLY_AUTOMATED", False)
    _auto_first   = _s.get("AUTO_FIRST_TRADE", False)
    _auto_first_n = _s.get("AUTO_FIRST_COUNT", config.AUTO_FIRST_COUNT)
    if not (_fully_auto or (_auto_first and trades_today() < _auto_first_n)):
        return
    for row in pending:
        sig_id = row["id"]
        try:
            execution = submit_entry(sig_id, broker=broker, requester="scheduler-retry")
            if execution["status"] != "pending":
                logger.info("Auto-entry succeeded on retry for signal #%d: %s", sig_id, execution["status"])
                notify._send(f"Entry #{sig_id}: {execution['status']} ({execution.get('execution_state') or 'paper'})")
                return   # one trade at a time — get_open_trades() re-checked next cycle
        except Exception as exc:
            logger.debug("Auto-entry retry #%d not yet ready: %s", sig_id, exc)


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
            exit_price = ltp
            try:
                outcome = request_exit(t['id'], "eod", requester="eod", index_price=exit_price)
                logger.info("EOD #%s: %s", t['id'], outcome['status'])
            except Exception as exc:
                logger.warning("EOD #%s unresolved: %s", t['id'], exc)

    path = export_day()
    logger.info("End of day export → %s", path)

    from journal.db import get_signals_for_date
    from datetime import date as _date
    today = _date.today().isoformat()
    closed = [dict(r) for r in get_signals_for_date(today) if r['status'] == 'closed']
    wins   = sum(1 for t in closed if t["result"] == "win")
    losses = sum(1 for t in closed if t["result"] == "loss")
    pnl    = daily_pnl()

    from journal.db import daily_rupee_accounting
    accounting = daily_rupee_accounting(today, mode='live')
    total_options_pnl = accounting['net'] if accounting['complete'] else None
    logger.info("Daily Index P&L: %.2f pts", pnl)
    notify.eod_summary(trades=len(closed), wins=wins, losses=losses,
                       total_pnl=pnl, total_options_pnl=total_options_pnl, accounting_basis=accounting["basis"])


def _simulate_signal_outcome(signal: dict, candles: list) -> dict:
    """Bar-by-bar simulate what would have happened if this signal was approved.

    Entry fill confirmation: the first bar at or after signal time must have traded
    through the entry price before we start tracking target/SL. If the entry price
    never traded in that bar (e.g. signal fired mid-bar and price moved away), the
    simulation returns 'unfilled' with 0 pnl — not counted as a win or loss.

    Outcomes (4-state framing from ZONE_AGENT_EVIDENCE_PACK.md):
      target       — zone held, price reached target before SL
      stoploss     — SL hit, but price recovered/stalled (zone was real, trade timing wrong)
      continuation — SL hit AND price continued past distal for 5+ bars (zone was false/consumed)
      eod          — neither target nor SL reached by end of session
      unfilled     — entry price never traded in the first bar after signal

    Returns sim_outcome and sim_pnl in index points.
    """
    sig_time   = signal["time_signal"]          # "HH:MM:SS"
    entry      = signal["entry"]
    sl         = signal["stop_loss"]
    target     = signal["intraday_target"]
    zone_class = signal["zone_class"]

    relevant = [c for c in candles if c.timestamp.strftime("%H:%M:%S") >= sig_time]
    if not relevant:
        return {"sim_outcome": "unfilled", "sim_pnl": 0.0}

    # Confirm the entry price traded in the first bar. For demand zones the price
    # must dip to entry (limit buy); for supply zones it must rise to entry (limit sell).
    first = relevant[0]
    if zone_class == "demand":
        filled = first.low <= entry
    else:
        filled = first.high >= entry

    if not filled:
        return {"sim_outcome": "unfilled", "sim_pnl": 0.0}

    sl_pnl     = round(sl - entry, 1)     if zone_class == "demand" else round(entry - sl, 1)
    target_pnl = round(target - entry, 1) if zone_class == "demand" else round(entry - target, 1)

    for idx, candle in enumerate(relevant):
        if zone_class == "demand":
            if candle.high >= target:
                return {"sim_outcome": "target",   "sim_pnl": target_pnl}
            if candle.low <= sl:
                # SL hit — check if price continued past distal (breakout, not reversal)
                distal     = signal.get("distal", sl)
                post_bars  = relevant[idx + 1: idx + 6]   # up to 5 bars after SL
                continued  = any(c.close < distal for c in post_bars)
                outcome    = "continuation" if continued else "stoploss"
                return {"sim_outcome": outcome, "sim_pnl": sl_pnl}
        else:
            if candle.low <= target:
                return {"sim_outcome": "target",   "sim_pnl": target_pnl}
            if candle.high >= sl:
                distal     = signal.get("distal", sl)
                post_bars  = relevant[idx + 1: idx + 6]
                continued  = any(c.close > distal for c in post_bars)
                outcome    = "continuation" if continued else "stoploss"
                return {"sim_outcome": outcome, "sim_pnl": sl_pnl}

    last_price = candles[-1].close if candles else entry
    eod_pnl = round((last_price - entry) if zone_class == "demand" else (entry - last_price), 1)
    return {"sim_outcome": "eod", "sim_pnl": eod_pnl}


def eod_signal_review():
    """After EOD: simulate outcomes of expired/rejected signals and send Telegram summary."""
    from journal.db import get_signals_for_date
    from datetime import date as _date

    today = _date.today().isoformat()
    all_signals = [dict(r) for r in get_signals_for_date(today)]
    if not all_signals:
        return

    try:
        candles = broker.get_historical(config.NIFTY_SYMBOL, "5minute", days=1)
    except Exception as e:
        logger.warning("EOD review: couldn't fetch candles — %s", e)
        return

    taken   = [s for s in all_signals if s["status"] == "closed"]
    skipped = [s for s in all_signals if s["status"] in ("expired", "rejected")]

    if not skipped and not taken:
        return

    simulated = []
    for sig in skipped:
        outcome = _simulate_signal_outcome(sig, candles)
        if outcome["sim_outcome"] == "unfilled":
            # Entry price never traded — no fill confirmation. Skip persisting; not valid training data.
            logger.info("EOD review #%d %s — unfilled (entry %.2f never traded)", sig["id"], sig["zone_class"], sig["entry"])
            continue
        simulated.append({**sig, **outcome})
        # Persist simulated outcome — ML training data for future model
        update_signal_sim_outcome(sig["id"], outcome["sim_outcome"], outcome["sim_pnl"])
        logger.info(
            "EOD review #%d %s %s — simulated: %s %+.1f pts (saved to DB)",
            sig["id"], sig["zone_type"], sig["zone_class"],
            outcome["sim_outcome"], outcome["sim_pnl"],
        )

    notify.eod_signal_review(taken, simulated)


def run():
    init_db()
    logger.info("Trading engine starting | mode=%s | broker=%s", config.MODE, config.BROKER)

    if not broker.is_connected():
        logger.error("Broker not connected. Run token refresh first.")
        return

    import telegram_handler
    telegram_handler.start_polling()

    # BUG 5 fix: removed every().day.at(SCAN_START) — the interval job already
    # fires at start, so keeping both caused a double-scan and potential duplicate signals.
    _scan_every = max(1, int(config.SCAN_INTERVAL_MINUTES))
    logger.info("Scan interval: every %d min", _scan_every)
    schedule.every(_scan_every).minutes.do(scan)
    schedule.every(1).minutes.do(monitor_open_trades)
    schedule.every(2).seconds.do(reconcile_open_trades)
    schedule.every(1).minutes.do(retry_pending_entries)
    schedule.every().day.at("15:20").do(end_of_day)         # 10 min before close
    schedule.every().day.at("15:30").do(eod_signal_review)  # simulate skipped signals
    # NOTE: backup does NOT run from here — the run() loop exits at 15:35 (see
    # below), so anything scheduled after that inside this same process can
    # never fire. Backup is its own cron line at 15:40 (crontab.example) that
    # calls `python -m backup` directly, independent of the engine process.

    logger.info("Scheduler running. Waiting for %s...", config.SCAN_START)

    _last_ltp        = None
    _flat_ticks      = 0          # consecutive 30s ticks with unchanged LTP
    _HOLIDAY_TICKS   = 30         # 30 × 30s = 15 min of no movement → holiday
    _last_holiday_check = 0.0

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
            # BUG 11 fix: end_of_day() is already scheduled at 15:20.
            # Only call it here if it somehow didn't run (open trades still exist),
            # to avoid a second EOD export + duplicate Telegram summary.
            if get_open_trades():
                end_of_day()
            config.ENGINE_PID_FILE.unlink(missing_ok=True)
            break

        # ── Holiday detection: no LTP movement for 15 min ─────────────────
        if is_market_open() and time.monotonic() - _last_holiday_check >= 30:
            _last_holiday_check = time.monotonic()
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

        time.sleep(2)  # Service the two-second execution reconciliation job.
