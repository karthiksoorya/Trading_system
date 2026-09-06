"""Explicit offline runner; never scheduled or imported by trade execution.

    py -3.12 -m agents.reflect_completed_trades --dry-run
    py -3.12 -m agents.reflect_completed_trades --date 2026-09-04
    py -3.12 agents/reflect_completed_trades.py --date 2026-09-04 --dry-run

Optional --db and --knowledge-path select an offline journal/store for inspection.
Defaults: data/trades.db and data/learning/knowledge_entries.jsonl. A store belongs
to one source journal (stable IDs use signals.id). Date filters use signals.date,
the journal's local signal date. Dry-run prints full candidate entries and a summary.
Existing IDs are skipped, including rows later filled in with additional premiums.
"""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.completed_trade_reflection import (  # noqa: E402
    DEFAULT_DB_PATH, normalize_trade_date, reflect_completed_trades,
)
from agents.knowledge_store import DEFAULT_PATH  # noqa: E402
from agents.knowledge_store import KnowledgeStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", type=normalize_trade_date,
                        help="Signal date YYYY-MM-DD (default: today, local journal date)")
    parser.add_argument("--from-date", type=normalize_trade_date)
    parser.add_argument("--to-date", type=normalize_trade_date)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print proposed entries without changing the knowledge store")
    parser.add_argument("--refresh-existing", action="store_true",
                        help="Explicitly recompute and refresh existing OBSERVED reflections")
    parser.add_argument("--deduplicate", action="store_true",
                        help="Remove duplicate physical JSONL IDs (no trade reflection run)")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH,
                        help="Existing journal SQLite database (opened read-only)")
    parser.add_argument("--knowledge-path", type=Path, default=DEFAULT_PATH,
                        help="Private machine JSONL store")
    args = parser.parse_args(argv)
    try:
        if args.deduplicate:
            removed, unique = KnowledgeStore(args.knowledge_path).deduplicate(dry_run=args.dry_run)
            print(json.dumps({"action": "deduplicate", "dry_run": args.dry_run,
                              "removed": removed, "unique": unique}))
            return 0
        reflect_completed_trades(db_path=args.db, trade_date=args.date,
                                 from_date=args.from_date, to_date=args.to_date,
                                 knowledge_path=args.knowledge_path, dry_run=args.dry_run,
                                 refresh_existing=args.refresh_existing)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Reflection failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
