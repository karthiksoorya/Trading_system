"""
agent/promote_memory.py — Promote a candidate memory file to live memory.json.

Enforces a shadow-validation gate before promotion is allowed:
  • MIN_SHADOW_DAYS distinct trading days
  • MIN_SHADOW_SIGNALS total signals evaluated
  • MIN_SKIP_OUTCOMES shadow-only SKIPs with resolved outcomes
During shadow mode the system evaluates every signal with BOTH live memory and the
candidate, logs differences to shadow_log.jsonl, and compares outcomes after close.

Usage:
    python3 agent/promote_memory.py                      # promote latest candidate
    python3 agent/promote_memory.py --date 2026-08-29    # promote specific date
    python3 agent/promote_memory.py --list               # list candidates + shadow stats
    python3 agent/promote_memory.py --force              # skip day-count gate
    python3 agent/promote_memory.py --shadow-report      # just show shadow stats, no promote
"""

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

_AGENT_DIR      = Path(__file__).parent
_MEMORY_PATH    = _AGENT_DIR / "memory.json"
_SHADOW_LOG     = _AGENT_DIR / "shadow_log.jsonl"
_ARCHIVE_DIR    = _AGENT_DIR / "memory_archive"
_MIN_SHADOW_DAYS    = 3   # distinct trading days
_MIN_SHADOW_SIGNALS = 10  # total signals seen
_MIN_SKIP_OUTCOMES  = 5   # shadow-only SKIPs with resolved outcomes
_OVERRIDE_LOG       = _AGENT_DIR / "promote_overrides.log"


# ── Helpers ───────────────────────────────────────────────────────────────────

def list_candidates() -> list[Path]:
    return sorted(_AGENT_DIR.glob("memory_candidate_*.json"), reverse=True)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_shadow_entries(candidate_date: str) -> list[dict]:
    """Load all shadow_log entries for a specific candidate date."""
    if not _SHADOW_LOG.exists():
        return []
    entries = []
    with _SHADOW_LOG.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                if e.get("candidate_date") == candidate_date:
                    entries.append(e)
            except Exception:
                pass
    return entries


def _shadow_stats(entries: list[dict]) -> dict:
    """
    Compute shadow performance stats.
    Joins with DB to get actual outcomes for each shadowed signal.
    """
    if not entries:
        return {"days": 0, "total": 0, "divergences": 0}

    # Distinct trading days seen
    days = len({e["date"] for e in entries})

    # Count verdicts
    total       = len(entries)
    divergences = sum(1 for e in entries if e["live_verdict"] != e["shadow_verdict"])

    # Agree rate
    agree_rate = round((total - divergences) / total * 100, 1) if total else 0

    # Join with DB for outcomes on signals that have closed
    signal_ids = list({e["signal_id"] for e in entries if e.get("signal_id")})
    outcomes: dict[int, str] = {}   # signal_id → win/loss/neutral
    pnl_map:  dict[int, float] = {}

    try:
        import sys as _sys
        _sys.path.insert(0, str(_AGENT_DIR.parent))
        import config as _cfg
        conn = sqlite3.connect(_cfg.DB_PATH)
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" * len(signal_ids))
        rows = conn.execute(f"""
            SELECT id,
                   COALESCE(result, sim_outcome) AS effective_result,
                   COALESCE(pnl_points, sim_pnl_points) AS pnl
            FROM signals
            WHERE id IN ({placeholders})
              AND (result IS NOT NULL OR sim_outcome IS NOT NULL)
        """, signal_ids).fetchall()
        conn.close()
        for row in rows:
            r = (row["effective_result"] or "").lower()
            if   r in ("win", "target"):   outcomes[row["id"]] = "win"
            elif r in ("loss", "stoploss"): outcomes[row["id"]] = "loss"
            else:                           outcomes[row["id"]] = "neutral"
            pnl_map[row["id"]] = row["pnl"] or 0
    except Exception:
        pass  # DB not available — show raw verdict stats only

    # For signals where shadow would have SKIP'd but live said TRADE:
    # what actually happened? (Did shadow correctly avoid a loss?)
    shadow_only_skips = [e for e in entries
                         if e["shadow_verdict"] == "SKIP" and e["live_verdict"] != "SKIP"]

    skip_outcomes = [outcomes[e["signal_id"]] for e in shadow_only_skips
                     if e.get("signal_id") in outcomes]
    skip_wins    = skip_outcomes.count("win")
    skip_losses  = skip_outcomes.count("loss")
    skip_neutral = skip_outcomes.count("neutral")
    skip_decided = skip_wins + skip_losses
    skip_wr      = round(skip_wins / skip_decided * 100, 1) if skip_decided else None

    # For signals both live and shadow said TRADE — baseline comparison
    both_trade = [e for e in entries
                  if e["shadow_verdict"] == "TRADE" and e["live_verdict"] == "TRADE"]
    trade_outcomes = [outcomes[e["signal_id"]] for e in both_trade
                      if e.get("signal_id") in outcomes]
    trade_wins    = trade_outcomes.count("win")
    trade_losses  = trade_outcomes.count("loss")
    trade_decided = trade_wins + trade_losses
    trade_wr      = round(trade_wins / trade_decided * 100, 1) if trade_decided else None

    return {
        "days":            days,
        "total":           total,
        "divergences":     divergences,
        "agree_rate":      agree_rate,
        "shadow_skips":    len(shadow_only_skips),
        "skip_outcomes":   skip_decided,
        "skip_wr":         skip_wr,
        "skip_wins":       skip_wins,
        "skip_losses":     skip_losses,
        "skip_neutral":    skip_neutral,
        "trade_wr":        trade_wr,
        "trade_decided":   trade_decided,
    }


def _print_shadow_report(candidate_date: str, stats: dict) -> None:
    print(f"\n  Shadow validation for candidate {candidate_date}:")
    print(f"  Trading days  : {stats['days']} / {_MIN_SHADOW_DAYS} required")
    print(f"  Signals seen  : {stats['total']} / {_MIN_SHADOW_SIGNALS} required")
    print(f"  Skip outcomes : {stats['skip_outcomes']} / {_MIN_SKIP_OUTCOMES} required")
    print(f"  Agreement    : {stats['agree_rate']}% with live memory")
    print(f"  Divergences  : {stats['divergences']}")

    if stats["shadow_skips"]:
        print(f"\n  Shadow-only SKIPs: {stats['shadow_skips']} signals")
        if stats["skip_outcomes"] > 0:
            verdict = ("✅ adding value" if (stats["skip_wr"] or 0) < (stats["trade_wr"] or 50) - 5
                       else "⚠️ skipping winners" if (stats["skip_wr"] or 0) > (stats["trade_wr"] or 50) + 5
                       else "— neutral so far")
            print(f"  Their WR      : {stats['skip_wr']}% ({stats['skip_wins']}W/"
                  f"{stats['skip_losses']}L) — {verdict}")
            if stats["trade_wr"] is not None:
                print(f"  Both-TRADE WR : {stats['trade_wr']}% ({stats['trade_decided']} signals)")
        else:
            print("  Outcomes not yet known (signals still open or not enough data)")
    else:
        print("  No shadow-only SKIPs yet — candidate agrees with live on all signals.")


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


# ── Promote ───────────────────────────────────────────────────────────────────

def promote(candidate_path: Path, force: bool = False) -> None:
    if not candidate_path.exists():
        print(f"ERROR: candidate not found: {candidate_path}")
        sys.exit(1)

    new_memory     = _load_json(candidate_path)
    old_memory: dict = {}
    if _MEMORY_PATH.exists():
        old_memory = _load_json(_MEMORY_PATH)

    candidate_date = candidate_path.stem.replace("memory_candidate_", "")
    shadow_entries = _load_shadow_entries(candidate_date)
    stats          = _shadow_stats(shadow_entries)

    # Hypothesis tracker summary
    ht            = new_memory.get("hypothesis_tracker", {})
    h_validated   = sum(1 for s in ht.values() for r in s.get("rules",[]) if r.get("status")=="historically_promising")
    h_rejected    = sum(1 for s in ht.values() for r in s.get("rules",[]) if r.get("status")=="rejected")
    h_testing     = sum(1 for s in ht.values() for r in s.get("rules",[]) if r.get("status")=="testing")
    h_untested    = sum(1 for s in ht.values() for r in s.get("rules",[]) if r.get("status")=="untested")

    print(f"\nCandidate : {candidate_path.name}")
    print(f"Trained   : {new_memory.get('last_trained', '?')}")
    print(f"Regime    : {new_memory.get('market_regime', '?')}")
    print(f"Mistakes  : {len(new_memory.get('mistake_log', []))} entries")
    print(f"Wins      : {len(new_memory.get('win_patterns', []))} entries")
    print(f"Cautions  : {len(new_memory.get('caution_flags', []))} entries")
    print(f"\nHypothesis tracker: ✅ {h_validated} validated | ❌ {h_rejected} rejected | "
          f"🔄 {h_testing} testing | ◇ {h_untested} untested")
    if h_validated:
        for s in ht.values():
            for r in s.get("rules", []):
                if r.get("status") == "historically_promising":
                    w, l = r.get("wins",0), r.get("losses",0)
                    wr = round(w/(w+l)*100) if (w+l) else 0
                    print(f"    ✅ {r['rule']} ({r.get('signals_tested',0)} signals, {wr}% WR)")

    _print_shadow_report(candidate_date, stats)

    print(f"\nChanges vs live memory.json:")
    print(_diff_summary(old_memory, new_memory))

    # ── Shadow validation gate ────────────────────────────────────────────
    gate_failures = []
    if stats["days"] < _MIN_SHADOW_DAYS:
        gate_failures.append(
            f"days={stats['days']} < {_MIN_SHADOW_DAYS} required"
        )
    if stats["total"] < _MIN_SHADOW_SIGNALS:
        gate_failures.append(
            f"signals={stats['total']} < {_MIN_SHADOW_SIGNALS} required"
        )
    if stats["skip_outcomes"] < _MIN_SKIP_OUTCOMES:
        gate_failures.append(
            f"skip_outcomes={stats['skip_outcomes']} < {_MIN_SKIP_OUTCOMES} required"
        )

    if gate_failures and not force:
        print(f"\n⛔ Promotion blocked:")
        for f in gate_failures:
            print(f"   • {f}")
        print(f"\n   The scheduler logs shadow verdicts to agent/shadow_log.jsonl each trading day.")
        print(f"   To bypass (experts only): python3 agent/promote_memory.py --force")
        return

    if gate_failures and force:
        # --force: require explicit typed confirmation + log the override
        print(f"\n⚠️  Gate failures being overridden by --force:")
        for f in gate_failures:
            print(f"   • {f}")
        confirm = input('\nType "FORCE" to confirm override: ').strip()
        if confirm != "FORCE":
            print("Aborted — typed confirmation did not match.")
            return
        reason = input("Reason for override (logged): ").strip() or "(no reason given)"
        import datetime as _dt
        with open(_OVERRIDE_LOG, "a") as _ol:
            _ol.write(
                f"{_dt.datetime.now().isoformat()} | candidate={candidate_path.name} | "
                f"failures={gate_failures} | reason={reason}\n"
            )
        print(f"Override logged to {_OVERRIDE_LOG}")

    # Always require y/N confirmation, UNLESS the FORCE typed confirmation already ran
    # (gate_failures AND force path). That way --force with all gates passing still asks.
    already_force_confirmed = bool(gate_failures) and force
    print(f"\nThis will overwrite: {_MEMORY_PATH}")
    if not already_force_confirmed:
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
    candidate_path.unlink()
    print(f"Removed candidate: {candidate_path.name}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Promote candidate memory to live memory.json")
    parser.add_argument("--date",          help="Specific candidate date (YYYY-MM-DD)")
    parser.add_argument("--list",          action="store_true", help="List candidates with shadow stats")
    parser.add_argument("--force",         action="store_true", help="Skip shadow day-count gate")
    parser.add_argument("--shadow-report", action="store_true", help="Show shadow stats without promoting")
    args = parser.parse_args()

    candidates = list_candidates()

    if args.list:
        if not candidates:
            print("No candidate files found.")
            return
        print(f"\n{'Candidate':<35} {'Days':>5} {'Sigs':>5} {'Agree':>7} {'ShadowSKIP WR':>14}")
        print("-" * 72)
        for c in candidates:
            try:
                d     = _load_json(c)
                cdate = c.stem.replace("memory_candidate_", "")
                ents  = _load_shadow_entries(cdate)
                st    = _shadow_stats(ents)
                skip_wr_str = f"{st['skip_wr']}%" if st.get("skip_wr") is not None else "n/a"
                print(f"{c.name:<35} {st['days']:>5} {st['total']:>5} "
                      f"{st['agree_rate']:>6}% {skip_wr_str:>14}")
            except Exception:
                print(f"{c.name:<35} (error)")
        return

    if args.date:
        target = _AGENT_DIR / f"memory_candidate_{args.date}.json"
    else:
        if not candidates:
            print("No candidate files found. Run trainer.py or seed_memory.py first.")
            sys.exit(1)
        target = candidates[0]
        print(f"Using latest candidate: {target.name}")

    if args.shadow_report:
        cdate = target.stem.replace("memory_candidate_", "")
        ents  = _load_shadow_entries(cdate)
        stats = _shadow_stats(ents)
        _print_shadow_report(cdate, stats)
        return

    promote(target, force=args.force)


if __name__ == "__main__":
    import sys as _sys
    _sys.path.insert(0, str(_AGENT_DIR.parent))
    try:
        from dotenv import load_dotenv
        load_dotenv(_AGENT_DIR.parent / ".env")
    except ImportError:
        pass
    main()
