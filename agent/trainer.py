"""
agent/trainer.py — EOD daily trainer.

Reads today's closed trades + current memory.json.
Calls Claude Sonnet to synthesise patterns and writes a CANDIDATE file
(memory_candidate_YYYY-MM-DD.json) — never overwrites live memory.json directly.
Run 'python3 agent/promote_memory.py' to review and promote the candidate.

Run via cron at 16:00 (after market close + EOD export):
    0 16 * * 1-5 source ~/Trading_system/venv/bin/activate && python3 ~/Trading_system/agent/trainer.py
"""

import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger(__name__)

_ROOT        = Path(__file__).parent.parent
_MEMORY_PATH = Path(__file__).parent / "memory.json"
_AGENT_DIR   = Path(__file__).parent
_MODEL       = "claude-sonnet-4-6"


def _load_memory() -> dict:
    if _MEMORY_PATH.exists():
        return json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
    return {
        "version": 1,
        "last_trained": None,
        "market_regime": "normal",
        "departure_thresholds": {"min": 1.0, "preferred": 1.5},
        "time_of_day_rules": {"prefer_before": "11:30", "avoid_after": "13:00"},
        "zone_type_notes": {},
        "mistake_log": [],
        "win_patterns": [],
        "caution_flags": [],
    }


def _load_today_trades() -> list[dict]:
    sys.path.insert(0, str(_ROOT))
    import sqlite3 as _sqlite3
    import config as _config
    conn = _sqlite3.connect(_config.DB_PATH)
    conn.row_factory = _sqlite3.Row
    cur = conn.execute("""
        SELECT zone_class, zone_type, timeframe,
               entry, stop_loss, intraday_target, status,
               booster_score, confluence_count,
               COALESCE(result, sim_outcome)         AS effective_result,
               COALESCE(pnl_points, sim_pnl_points)  AS pnl_points,
               CASE WHEN result IS NOT NULL THEN 'actual' ELSE 'simulated' END AS data_type,
               options_entry_price, options_exit_price, options_lot_size,
               exit_reason, time_signal
        FROM signals
        WHERE date = ?
          AND (result IS NOT NULL OR sim_outcome IS NOT NULL)
    """, (date.today().isoformat(),))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    # Compute net options P&L (gross minus execution costs) where data exists
    from agent.costs import net_options_pnl as _net_pnl, estimate_cost as _est_cost
    for row in rows:
        ep  = row.get("options_entry_price") or 0
        xp  = row.get("options_exit_price")  or 0
        lot = row.get("options_lot_size")     or 0
        if ep and xp and lot:
            row["net_options_pnl_rs"] = _net_pnl(ep, xp, lot)
            row["execution_cost_rs"]  = _est_cost(ep, xp, lot)
        else:
            row["net_options_pnl_rs"] = None
            row["execution_cost_rs"]  = None

    return rows


def _classify_result(effective_result: str | None) -> str:
    """Standardise outcome into win / loss / neutral."""
    r = (effective_result or "").lower()
    if r in ("win", "target"):
        return "win"
    if r in ("loss", "stoploss"):
        return "loss"
    return "neutral"  # breakeven, eod, manual, unknown


def _build_prompt(trades: list[dict], memory: dict) -> str:
    today = date.today().isoformat()

    if not trades:
        trades_text = "No signals today (no closed trades or simulated outcomes)."
        actual_ct = sim_ct = wins = losses = neutrals = 0
    else:
        actual_ct = sum(1 for t in trades if t["data_type"] == "actual")
        sim_ct    = sum(1 for t in trades if t["data_type"] == "simulated")
        wins      = sum(1 for t in trades if _classify_result(t.get("effective_result")) == "win")
        losses    = sum(1 for t in trades if _classify_result(t.get("effective_result")) == "loss")
        neutrals  = len(trades) - wins - losses
        lines     = [
            f"  ({actual_ct} actual trades + {sim_ct} simulated skipped signals)",
            f"  Outcomes: {wins} wins / {losses} losses / {neutrals} neutral (breakeven/eod)",
        ]
        for t in trades:
            tag     = "[ACTUAL]" if t["data_type"] == "actual" else "[SIMULATED]"
            outcome = _classify_result(t.get("effective_result"))
            if t.get("net_options_pnl_rs") is not None:
                opts = (f"  Options P&L=₹{t['net_options_pnl_rs']:+,.0f}"
                        f" (after ₹{t.get('execution_cost_rs',0):.0f} costs)")
            else:
                opts = ""
            lines.append(
                f"  {tag} [{t['zone_class'].upper()} {t['zone_type']} {t['timeframe']}]"
                f"  Booster={t.get('booster_score',0):.1f}"
                f"  Confluence={t.get('confluence_count',1)}"
                f"  Result={outcome.upper()}"
                f"  PnL={t.get('pnl_points', 0):+.1f}pts"
                f"  ExitReason={t.get('exit_reason','?')}"
                f"  Time={t.get('time_signal','?')}"
                f"{opts}"
            )
        trades_text = "\n".join(lines)

    return f"""You are the daily trainer for a NIFTY demand/supply zone intraday options trading system.

TODAY'S SIGNALS ([ACTUAL] = approved and closed, [SIMULATED] = skipped but bar-by-bar
simulated after EOD — both included to avoid selection bias):
{trades_text}

CURRENT MEMORY:
{json.dumps(memory, indent=2)}

Your task: update the memory JSON based on today's outcomes.

CRITICAL RULES:
1. Only modify: mistake_log, win_patterns, caution_flags, departure_thresholds, time_of_day_rules, zone_type_notes, last_trained.
2. market_regime: DO NOT derive from today's win rate. It must come from observable market context
   (e.g. trending vs choppy price action, VIX level, broad market direction). If you cannot infer
   regime from the data provided, keep the existing value unchanged.
3. mistake_log: add a pattern note only when 3+ losses share a clear common factor. Max 10 items.
4. win_patterns: add a pattern note only when 3+ wins share a clear common factor. Max 10 items.
5. caution_flags: set active warnings with evidence from today's data. Max 5.
6. Breakeven and EOD exits are NEUTRAL — do not count them as losses or wins in any pattern.
7. IMPORTANT: from 1-2 losing trades, add a caution_flag, NOT a mistake_log entry.
   mistake_log is for confirmed patterns across multiple samples.
8. Set last_trained to today: {today}
9. If today had fewer than 3 closed trades, only update last_trained and caution_flags if a clear
   issue appeared — do not update thresholds or add patterns from tiny samples.

Reply with ONLY the updated JSON object. No markdown, no explanation, no code fences. Raw JSON only."""


def _update_hypothesis_tracker(memory: dict, today: str) -> dict:
    """
    For each testable hypothesis filter in agent/knowledge/*.json,
    count today's signal matches and update win/loss tallies.
    Promotes hypothesis status when 20+ signals have been tested.
    """
    import sqlite3 as _sq
    from pathlib import Path as _Path

    kb_dir  = Path(__file__).parent / "knowledge"
    tracker = {k: v for k, v in memory.get("hypothesis_tracker", {}).items()}

    try:
        import config as _cfg
        conn = _sq.connect(_cfg.DB_PATH)
        conn.row_factory = _sq.Row

        # Baseline WR for today (wins vs losses, neutral excluded)
        baseline = conn.execute("""
            SELECT
                SUM(CASE WHEN COALESCE(result, sim_outcome) IN ('win','target')    THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN COALESCE(result, sim_outcome) IN ('loss','stoploss') THEN 1 ELSE 0 END) AS losses
            FROM signals
            WHERE date = ? AND (result IS NOT NULL OR sim_outcome IS NOT NULL)
        """, (today,)).fetchone()

        b_wins   = baseline["wins"]   or 0
        b_losses = baseline["losses"] or 0
        b_total  = b_wins + b_losses
        b_wr     = b_wins / b_total if b_total else 0.5

        for kf in sorted(kb_dir.glob("*.json")):
            try:
                kd = json.loads(kf.read_text(encoding="utf-8"))
            except Exception:
                continue

            slug    = kf.stem
            filters = [sf for sf in kd.get("signal_filters", [])
                       if sf.get("testable") and sf.get("filter")]
            if not filters:
                continue

            if slug not in tracker:
                tracker[slug] = {"source": kd.get("label", slug), "rules": []}

            existing_rules = {r["rule"]: r for r in tracker[slug].get("rules", [])}

            for sf in filters:
                rule_text  = sf["rule"]
                sql_filter = sf["filter"]
                entry      = existing_rules.get(rule_text) or {
                    "rule":           rule_text,
                    "filter":         sql_filter,
                    "signals_tested": 0,
                    "wins":           0,
                    "losses":         0,
                    "status":         "untested",
                }
                try:
                    row = conn.execute(f"""
                        SELECT
                            SUM(CASE WHEN COALESCE(result, sim_outcome) IN ('win','target')    THEN 1 ELSE 0 END) AS wins,
                            SUM(CASE WHEN COALESCE(result, sim_outcome) IN ('loss','stoploss') THEN 1 ELSE 0 END) AS losses
                        FROM signals
                        WHERE date = ?
                          AND (result IS NOT NULL OR sim_outcome IS NOT NULL)
                          AND ({sql_filter})
                    """, (today,)).fetchone()

                    day_wins   = row["wins"]   or 0
                    day_losses = row["losses"] or 0
                    entry["signals_tested"] += day_wins + day_losses
                    entry["wins"]           += day_wins
                    entry["losses"]         += day_losses

                    total = entry["signals_tested"]
                    wins  = entry["wins"]
                    dec   = wins + entry["losses"]
                    wr    = wins / dec if dec else 0

                    if total >= 20:
                        if   wr >= b_wr + 0.10:  entry["status"] = "validated"
                        elif wr <= b_wr - 0.10:  entry["status"] = "rejected"
                        else:                     entry["status"] = "inconclusive"
                    elif total >= 5:
                        entry["status"] = "testing"
                    else:
                        entry["status"] = "untested"

                except Exception as e:
                    logger.debug("Hypothesis filter error for '%s': %s", rule_text, e)

                existing_rules[rule_text] = entry

            tracker[slug]["rules"] = list(existing_rules.values())

        conn.close()
    except Exception as e:
        logger.warning("Hypothesis tracker update failed (non-fatal): %s", e)

    return tracker


def run() -> None:
    logger.info("=== Daily Trainer starting ===")

    try:
        import anthropic
    except ImportError:
        logger.error("anthropic not installed — run: pip install anthropic")
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set")
        return

    memory = _load_memory()
    logger.info("Memory loaded. Last trained: %s", memory.get("last_trained", "never"))

    try:
        trades = _load_today_trades()
        logger.info("Today's signals (actual + simulated): %d", len(trades))
    except Exception as e:
        logger.error("Could not load trades: %s", e)
        trades = []

    prompt = _build_prompt(trades, memory)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
    except Exception as e:
        logger.error("Claude API call failed: %s", e)
        return

    try:
        updated = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("Claude returned invalid JSON: %s\nRaw: %.300s", e, raw)
        return

    # Update hypothesis tracker with today's signal confirmations
    today_str = date.today().isoformat()
    updated["hypothesis_tracker"] = _update_hypothesis_tracker(updated, today_str)

    validated = sum(
        1 for src in updated["hypothesis_tracker"].values()
        for r in src.get("rules", []) if r.get("status") == "validated"
    )
    rejected = sum(
        1 for src in updated["hypothesis_tracker"].values()
        for r in src.get("rules", []) if r.get("status") == "rejected"
    )
    logger.info("Hypothesis tracker: %d validated, %d rejected", validated, rejected)

    # Write CANDIDATE — never overwrite live memory directly
    candidate_path = _AGENT_DIR / f"memory_candidate_{today_str}.json"
    candidate_path.write_text(json.dumps(updated, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info("Candidate memory written → %s", candidate_path)
    logger.info("Review it, then run: python3 agent/promote_memory.py")
    logger.info(
        "Cautions: %d | Mistakes: %d | Win patterns: %d | Last trained: %s",
        len(updated.get("caution_flags", [])),
        len(updated.get("mistake_log", [])),
        len(updated.get("win_patterns", [])),
        updated.get("last_trained"),
    )
    logger.info("=== Daily Trainer complete ===")


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(_ROOT / ".env")
    except ImportError:
        pass
    run()
