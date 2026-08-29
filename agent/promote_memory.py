"""
agent/promote_memory.py — Promote a candidate memory file to live memory.json.

After trainer.py or seed_memory.py creates a memory_candidate_DATE.json,
review it then run this script to promote it to live memory.

Usage:
    python3 agent/promote_memory.py                      # promote latest candidate
    python3 agent/promote_memory.py --date 2026-08-29    # promote specific date
    python3 agent/promote_memory.py --list               # list available candidates
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

_AGENT_DIR   = Path(__file__).parent
_MEMORY_PATH = _AGENT_DIR / "memory.json"
_ARCHIVE_DIR = _AGENT_DIR / "memory_archive"


def list_candidates() -> list[Path]:
    return sorted(_AGENT_DIR.glob("memory_candidate_*.json"), reverse=True)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _diff_summary(old: dict, new: dict) -> str:
    lines = []
    fields = ["market_regime", "departure_thresholds", "time_of_day_rules",
              "mistake_log", "win_patterns", "caution_flags"]
    for f in fields:
        ov = old.get(f)
        nv = new.get(f)
        if ov != nv:
            if isinstance(nv, list):
                lines.append(f"  {f}: {len(ov or [])} → {len(nv)} items")
            else:
                lines.append(f"  {f}: {ov!r} → {nv!r}")
    return "\n".join(lines) if lines else "  (no changes detected)"


def promote(candidate_path: Path, force: bool = False) -> None:
    if not candidate_path.exists():
        print(f"ERROR: candidate not found: {candidate_path}")
        sys.exit(1)

    new_memory = _load_json(candidate_path)

    # Load current live memory (if any)
    old_memory: dict = {}
    if _MEMORY_PATH.exists():
        old_memory = _load_json(_MEMORY_PATH)

    print(f"\nCandidate : {candidate_path.name}")
    print(f"Trained   : {new_memory.get('last_trained', '?')}")
    print(f"Regime    : {new_memory.get('market_regime', '?')}")
    print(f"Mistakes  : {len(new_memory.get('mistake_log', []))} entries")
    print(f"Wins      : {len(new_memory.get('win_patterns', []))} entries")
    print(f"Cautions  : {len(new_memory.get('caution_flags', []))} entries")
    print(f"\nChanges vs live memory.json:")
    print(_diff_summary(old_memory, new_memory))

    print(f"\nThis will overwrite: {_MEMORY_PATH}")
    if not force:
        confirm = input("Promote to live? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            return

    # Archive current memory.json before overwriting
    if _MEMORY_PATH.exists():
        _ARCHIVE_DIR.mkdir(exist_ok=True)
        ts          = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_dst = _ARCHIVE_DIR / f"memory_archive_{ts}.json"
        archive_dst.write_bytes(_MEMORY_PATH.read_bytes())
        print(f"Archived old memory → {archive_dst}")

    _MEMORY_PATH.write_text(
        json.dumps(new_memory, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Promoted  → {_MEMORY_PATH}")

    # Clean up the candidate file
    candidate_path.unlink()
    print(f"Removed candidate file: {candidate_path.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Promote candidate memory to live memory.json")
    parser.add_argument("--date",  help="Specific candidate date (YYYY-MM-DD)")
    parser.add_argument("--list",  action="store_true", help="List available candidates")
    parser.add_argument("--force", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    candidates = list_candidates()

    if args.list:
        if not candidates:
            print("No candidate files found.")
            return
        print("\nAvailable candidates:")
        for c in candidates:
            try:
                d = _load_json(c)
                print(f"  {c.name}  (regime={d.get('market_regime','?')}, "
                      f"trained={d.get('last_trained','?')})")
            except Exception:
                print(f"  {c.name}  (parse error)")
        return

    if args.date:
        target = _AGENT_DIR / f"memory_candidate_{args.date}.json"
    else:
        if not candidates:
            print("No candidate files found. Run trainer.py or seed_memory.py first.")
            sys.exit(1)
        target = candidates[0]
        print(f"Using latest candidate: {target.name}")

    promote(target, force=args.force)


if __name__ == "__main__":
    main()
