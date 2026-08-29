"""
agent/seed_memory.py — One-time historical memory seeder.

Reads ALL closed trades from trades.db, aggregates patterns, reads
AGENT_KNOWLEDGE.md (if present), and asks Claude Sonnet to synthesise
everything into a CANDIDATE memory file (memory_candidate_YYYY-MM-DD.json).

IMPORTANT: This does NOT overwrite memory.json directly.
Review the candidate, then run: python3 agent/promote_memory.py

Usage:
    python3 agent/seed_memory.py
"""

import json
import logging
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger(__name__)

_ROOT        = Path(__file__).parent.parent
_MEMORY_PATH = Path(__file__).parent / "memory.json"
_AGENT_DIR   = Path(__file__).parent
_DB_PATH     = _ROOT / "data" / "trades.db"
_KNOWLEDGE   = _ROOT / "AGENT_KNOWLEDGE.md"
_MODEL       = "claude-sonnet-4-6"


# ── Classify outcome ──────────────────────────────────────────────────────────

def _classify(effective_result: str | None) -> str:
    r = (effective_result or "").lower()
    if r in ("win", "target"):
        return "win"
    if r in ("loss", "stoploss"):
        return "loss"
    return "neutral"   # breakeven, eod, manual, unknown


# ── Load all closed trades ────────────────────────────────────────────────────

def _load_all_trades() -> list[dict]:
    if not _DB_PATH.exists():
        logger.error("trades.db not found at %s", _DB_PATH)
        return []
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("""
        SELECT zone_class, zone_type, timeframe,
               entry, stop_loss, intraday_target, status,
               booster_score, confluence_count,
               COALESCE(result, sim_outcome)        AS effective_result,
               COALESCE(pnl_points, sim_pnl_points) AS pnl,
               CASE WHEN result IS NOT NULL THEN 'actual' ELSE 'simulated' END AS data_type,
               options_entry_price, options_exit_price, options_lot_size,
               exit_reason, time_signal, date
        FROM signals
        WHERE result IS NOT NULL OR sim_outcome IS NOT NULL
        ORDER BY date ASC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    # Compute net options P&L where data exists
    for row in rows:
        ep  = row.get("options_entry_price") or 0
        xp  = row.get("options_exit_price")  or 0
        lot = row.get("options_lot_size")     or 0
        if ep and xp and lot:
            row["net_options_pnl_rs"] = round((xp - ep) * lot, 2)
        else:
            row["net_options_pnl_rs"] = None

    return rows


# ── Aggregate stats ───────────────────────────────────────────────────────────

def _aggregate(trades: list[dict]) -> dict:
    actual    = [t for t in trades if t["data_type"] == "actual"]
    simulated = [t for t in trades if t["data_type"] == "simulated"]
    total     = len(trades)

    def _stats(rows):
        wins    = sum(1 for t in rows if _classify(t["effective_result"]) == "win")
        losses  = sum(1 for t in rows if _classify(t["effective_result"]) == "loss")
        neutral = len(rows) - wins - losses
        wr      = round(wins / (wins + losses) * 100, 1) if (wins + losses) else 0
        return wins, losses, neutral, wr

    # By zone type (combined actual + simulated)
    by_type: dict[str, dict] = defaultdict(lambda: {"wins": 0, "losses": 0, "neutral": 0, "pnl": 0.0})
    for t in trades:
        k      = f"{t['zone_class'].upper()} {t['zone_type']}"
        outcome = _classify(t["effective_result"])
        by_type[k]["wins"]    += 1 if outcome == "win"  else 0
        by_type[k]["losses"]  += 1 if outcome == "loss" else 0
        by_type[k]["neutral"] += 1 if outcome == "neutral" else 0
        by_type[k]["pnl"]     += t["pnl"] or 0

    type_summary = []
    for k, v in sorted(by_type.items()):
        decided = v["wins"] + v["losses"]
        wr      = round(v["wins"] / decided * 100) if decided else 0
        type_summary.append(
            f"  {k}: {decided+v['neutral']} total ({decided} decided, {v['neutral']} neutral) "
            f"{wr}% WR (wins vs losses), {v['pnl']:+.1f}pts PnL"
        )

    # By exit reason (actual trades only)
    by_exit: dict[str, int] = defaultdict(int)
    for t in actual:
        by_exit[t["exit_reason"] or "unknown"] += 1
    exit_summary = [f"  {k}: {v}" for k, v in sorted(by_exit.items(), key=lambda x: -x[1])]

    # By time of day (all signals)
    by_hour: dict[int, dict] = defaultdict(lambda: {"wins": 0, "losses": 0, "neutral": 0})
    for t in trades:
        try:
            h       = int((t["time_signal"] or "09:15:00")[:2])
            outcome = _classify(t["effective_result"])
            by_hour[h][outcome + "s"] += 1
        except Exception:
            pass

    hour_summary = []
    for h in sorted(by_hour):
        v       = by_hour[h]
        decided = v.get("wins", 0) + v.get("losses", 0)
        wr      = round(v.get("wins", 0) / decided * 100) if decided else 0
        hour_summary.append(
            f"  {h:02d}:xx  total={decided+v.get('neutral',0)}"
            f"  decided={decided}  {wr}% WR  neutral={v.get('neutral',0)}"
        )

    # Options P&L (actual trades with data)
    opts_trades = [t for t in actual if t.get("net_options_pnl_rs") is not None]
    if opts_trades:
        total_opts_pnl = sum(t["net_options_pnl_rs"] for t in opts_trades)
        opts_summary   = (f"  {len(opts_trades)} trades with options data, "
                          f"total net P&L = ₹{total_opts_pnl:+,.0f}")
    else:
        opts_summary = "  (no options P&L data available)"

    # By zone class
    demand_trades = [t for t in trades if t["zone_class"] == "demand"]
    supply_trades = [t for t in trades if t["zone_class"] == "supply"]

    a_wins, a_losses, a_neutral, a_wr = _stats(actual)
    s_wins, s_losses, s_neutral, s_wr = _stats(simulated)

    return {
        "summary":   (f"{total} total | "
                      f"{len(actual)} actual ({a_wins}W/{a_losses}L/{a_neutral}N, {a_wr}% WR) | "
                      f"{len(simulated)} simulated ({s_wins}W/{s_losses}L/{s_neutral}N, {s_wr}% WR)"),
        "by_type":   "\n".join(type_summary) or "  (none)",
        "by_exit":   "\n".join(exit_summary)  or "  (none)",
        "by_hour":   "\n".join(hour_summary)  or "  (none)",
        "opts_pnl":  opts_summary,
        "demand_wr": (f"{len(demand_trades)} signals, "
                      f"{_stats(demand_trades)[3]}% WR (wins vs decided)"),
        "supply_wr": (f"{len(supply_trades)} signals, "
                      f"{_stats(supply_trades)[3]}% WR (wins vs decided)"),
        "actual_ct": len(actual),
        "sim_ct":    len(simulated),
    }


# ── Read knowledge ────────────────────────────────────────────────────────────

def _load_knowledge() -> str:
    if _KNOWLEDGE.exists():
        text = _KNOWLEDGE.read_text(encoding="utf-8")
        if len(text) > 4000:
            text = text[:4000] + "\n...[truncated]"
        return text
    return "(AGENT_KNOWLEDGE.md not found — skipping)"


# ── Build prompt ──────────────────────────────────────────────────────────────

def _build_prompt(agg: dict, knowledge: str, ext_knowledge: str, current_memory: dict) -> str:
    today = datetime.today().strftime("%Y-%m-%d")
    return f"""You are seeding the long-term memory for a NIFTY intraday demand/supply zone options trading agent.

HISTORICAL SIGNAL DATA ({agg['actual_ct']} actual trades + {agg['sim_ct']} simulated skipped signals):
NOTE: "actual" = approved and closed. "simulated" = skipped/rejected but bar-by-bar simulated after EOD.
Both used to avoid selection bias. WR = wins / (wins + losses) — neutral outcomes excluded from WR.
"Neutral" = breakeven, EOD, or manual exits where outcome was inconclusive.

Overall: {agg['summary']}

By zone type:
{agg['by_type']}

By exit reason (actual trades):
{agg['by_exit']}

By time of day (hour):
{agg['by_hour']}

Options P&L (actual trades):
{agg['opts_pnl']}

Demand zones: {agg['demand_wr']}
Supply zones:  {agg['supply_wr']}

TRADER'S WRITTEN KNOWLEDGE (AGENT_KNOWLEDGE.md):
{knowledge}

EXTERNAL REFERENCE KNOWLEDGE (hypotheses from ingested sources — not yet validated against this system):
{ext_knowledge}

CURRENT MEMORY (to merge into, not replace blindly):
{json.dumps(current_memory, indent=2)}

Your task: produce a rich, updated memory JSON that captures real patterns from the data above.

CRITICAL GUIDELINES:
1. market_regime: set based on observable market conditions implied by the data (price action
   characteristics, time period, VIX patterns IF mentioned in knowledge). DO NOT derive from
   win rate alone. Win rate reflects strategy edge, NOT market regime. Default to "normal" if unclear.
2. departure_thresholds: set based on what the zone quality data shows, or keep existing.
3. time_of_day_rules: set from the hourly data — identify statistically strong windows.
   Only flag a time as "avoid" if there are 5+ samples showing consistent losses.
4. mistake_log: populate with patterns that have 5+ supporting examples. Max 5 entries.
   Format: "Zone type + condition + outcome — N examples"
5. win_patterns: populate with patterns that have 5+ supporting examples. Max 5 entries.
6. caution_flags: set the most important active warnings (max 5). Can have smaller sample backing
   but must be clearly specific (not generic advice).
7. zone_type_notes: 1-2 sentence note per zone type with meaningful data (5+ trades).
8. External knowledge items are HYPOTHESES (not validated for this specific system).
   Reference them only when they align with the actual data above. Label them as hypothesis.
9. Set last_trained to today: {today}
10. Be specific — reference the actual numbers from the data, not generic trading advice.

Reply with ONLY the updated JSON object. No markdown, no explanation. Raw JSON only."""


# ── Main ──────────────────────────────────────────────────────────────────────

def run() -> None:
    logger.info("=== Historical Memory Seeder starting ===")

    try:
        import anthropic
    except ImportError:
        logger.error("anthropic not installed — run: pip install anthropic")
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set")
        return

    trades = _load_all_trades()
    logger.info("Loaded %d signals from trades.db", len(trades))
    if not trades:
        logger.error("No signals found — nothing to seed from")
        return

    agg       = _aggregate(trades)
    knowledge = _load_knowledge()
    logger.info("Knowledge file: %d chars", len(knowledge))

    current_memory: dict = {}
    if _MEMORY_PATH.exists():
        try:
            current_memory = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    print(f"\n{agg['summary']}")
    today_str      = datetime.today().strftime("%Y-%m-%d")
    candidate_path = _AGENT_DIR / f"memory_candidate_{today_str}.json"
    print(f"This will CREATE a CANDIDATE file: {candidate_path}")
    print("(memory.json will NOT be touched — use 'python3 agent/promote_memory.py' to promote)")
    confirm = input("Proceed? [y/N] ").strip().lower()
    if confirm != "y":
        logger.info("Aborted.")
        return

    # Load ingested external knowledge
    from agent.ingest import load_all_knowledge
    ext_knowledge = load_all_knowledge()
    logger.info("External knowledge: %d chars", len(ext_knowledge))

    prompt = _build_prompt(agg, knowledge, ext_knowledge, current_memory)

    logger.info("Calling Claude Sonnet to synthesise memory ...")
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=_MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
    except Exception as e:
        logger.error("Claude API call failed: %s", e)
        return

    try:
        updated = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("Claude returned invalid JSON: %s\nRaw: %.400s", e, raw)
        return

    candidate_path.write_text(json.dumps(updated, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Candidate written → %s", candidate_path)
    logger.info(
        "Regime: %s | Cautions: %d | Mistakes: %d | Win patterns: %d",
        updated.get("market_regime"),
        len(updated.get("caution_flags", [])),
        len(updated.get("mistake_log", [])),
        len(updated.get("win_patterns", [])),
    )
    logger.info("=== Seeder complete — review candidate, then run promote_memory.py ===")


if __name__ == "__main__":
    sys.path.insert(0, str(_ROOT))
    try:
        from dotenv import load_dotenv
        load_dotenv(_ROOT / ".env")
    except ImportError:
        pass
    run()
