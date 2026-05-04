import csv
import logging
from datetime import date
from pathlib import Path
from typing import Optional

import config
from journal.db import get_signals_for_date

logger = logging.getLogger(__name__)

# Matches the Paper Trade CSV schema from the master doc
_COLUMNS = [
    "id", "date", "time_signal", "zone_type", "zone_class", "timeframe",
    "proximal", "distal", "entry", "stop_loss",
    "intraday_target", "overnight_target",
    "booster_score", "freshness", "strength", "time_score", "rr_score",
    "entry_type", "position_size",
    "confluence_count", "confluence_tfs",
    "exit_time", "exit_price", "exit_reason",
    "pnl_points", "result", "rule_based", "notes", "mode",
]


def export_day(trade_date: Optional[str] = None) -> Path:
    """Export all signals for a given date to CSV. Returns the file path."""
    trade_date = trade_date or date.today().isoformat()
    rows = get_signals_for_date(trade_date)

    config.CSV_DIR.mkdir(parents=True, exist_ok=True)
    out_path = config.CSV_DIR / f"trades_{trade_date}.csv"

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))

    logger.info("Exported %d signal(s) to %s", len(rows), out_path)
    return out_path
