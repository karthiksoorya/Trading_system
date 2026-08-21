"""
Self-learning: analyses closed trade outcomes every 10 trades.
Auto-disables zone types and timeframes with win rate < 35% over 10+ trades.
Never auto-enables — only a human can re-enable via the dashboard.
Logs all decisions to logs/autolearn.log.
"""

import logging
import sqlite3
from logging.handlers import RotatingFileHandler

import config

WIN_RATE_THRESHOLD = 35.0   # % — below this triggers auto-disable
MIN_TRADES         = 10     # minimum trades per group before auto-disable

LOG_DIR  = config.BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "autolearn.log"
LOG_DIR.mkdir(exist_ok=True)

_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
logger = logging.getLogger(__name__)
logger.addHandler(_handler)
logger.setLevel(logging.INFO)


def check_and_learn():
    """Public entry point — call after every trade closes."""
    try:
        _run()
    except Exception as e:
        logger.error("check_and_learn error: %s", e)


def _run():
    import notify
    from datetime import date, timedelta

    # BUG 22 fix: analyse only the last 90 days of trades instead of full history.
    # Old trades from a different market regime or before settings changes were pulling
    # down win rates for zone types that are now performing well.
    RECENCY_DAYS = 90
    cutoff = (date.today() - timedelta(days=RECENCY_DAYS)).isoformat()

    con = sqlite3.connect(config.DB_PATH)
    rows = con.execute(
        "SELECT zone_type, timeframe, pnl_points FROM signals WHERE result IS NOT NULL AND date >= ?",
        (cutoff,),
    ).fetchall()
    con.close()

    total = len(rows)
    if total == 0:
        return

    # Only analyse at exact multiples of MIN_TRADES (10, 20, 30 …)
    if total % MIN_TRADES != 0:
        return

    logger.info("Auto-learn triggered at %d closed trades (last %d days).", total, RECENCY_DAYS)

    settings            = config.load_settings()
    disabled_zone_types = set(settings.get("DISABLED_ZONE_TYPES", []))
    scan_tfs            = list(settings.get("SCAN_TIMEFRAMES",
                               [config.TF_LOWER, config.TF_INTERMEDIATE, config.TF_HIGHER]))
    changed             = False

    # ── Zone type analysis ────────────────────────────────────────────────
    zone_stats: dict[str, dict] = {}
    for zone_type, _, pnl in rows:
        if zone_type not in zone_stats:
            zone_stats[zone_type] = {"trades": 0, "wins": 0}
        zone_stats[zone_type]["trades"] += 1
        if pnl and pnl > 0:
            zone_stats[zone_type]["wins"] += 1

    for zt, s in zone_stats.items():
        if s["trades"] < MIN_TRADES:
            continue
        win_rate = s["wins"] / s["trades"] * 100
        if win_rate < WIN_RATE_THRESHOLD and zt not in disabled_zone_types:
            disabled_zone_types.add(zt)
            changed = True
            msg = (
                f"Auto-disabled zone type {zt} — "
                f"{win_rate:.0f}% win rate on {s['trades']} trades "
                f"(threshold {WIN_RATE_THRESHOLD:.0f}%)"
            )
            logger.info(msg)
            notify.autolearn_alert(msg)

    # ── Timeframe analysis ────────────────────────────────────────────────
    tf_stats: dict[str, dict] = {}
    for _, timeframe, pnl in rows:
        if timeframe not in tf_stats:
            tf_stats[timeframe] = {"trades": 0, "wins": 0}
        tf_stats[timeframe]["trades"] += 1
        if pnl and pnl > 0:
            tf_stats[timeframe]["wins"] += 1

    for tf, s in tf_stats.items():
        if s["trades"] < MIN_TRADES:
            continue
        win_rate = s["wins"] / s["trades"] * 100
        if win_rate < WIN_RATE_THRESHOLD and tf in scan_tfs:
            scan_tfs.remove(tf)
            changed = True
            msg = (
                f"Auto-disabled timeframe {tf} — "
                f"{win_rate:.0f}% win rate on {s['trades']} trades "
                f"(threshold {WIN_RATE_THRESHOLD:.0f}%)"
            )
            logger.info(msg)
            notify.autolearn_alert(msg)

    if changed:
        config.save_settings({
            "DISABLED_ZONE_TYPES": list(disabled_zone_types),
            "SCAN_TIMEFRAMES":     scan_tfs,
        })
        logger.info(
            "Settings updated — disabled zone types: %s | active TFs: %s",
            list(disabled_zone_types), scan_tfs,
        )
    else:
        logger.info(
            "No changes at %d trades — all performers above %.0f%% threshold.",
            total, WIN_RATE_THRESHOLD,
        )
