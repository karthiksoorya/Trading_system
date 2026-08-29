"""
agent/seed_memory.py — One-time historical memory seeder.

Reads ALL closed trades from trades.db, aggregates patterns, reads
AGENT_KNOWLEDGE.md (if present), and asks Claude Sonnet to synthesise
everything into a rich initial memory.json.

Run once on the VPS after deploying agentic-v2:
    python3 agent/seed_memory.py

Safe to re-run — it asks for confirmation before overwriting memory.json.
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
_DB_PATH     = _ROOT / "data" / "trades.db"
_KNOWLEDGE   = _ROOT / "AGENT_KNOWLEDGE.md"
_MODEL       = "claude-sonnet-4-6"


# ── Load all closed trades from SQLite directly ───────────────────────────────

def _load_all_trades() -> list[dict]:
    if not _DB_PATH.exists():
        logger.error("trades.db not found at %s", _DB_PATH)
        return []
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("""
        SELECT zone_class, zone_type, timeframe,
               entry, stop_loss, intraday_target,
               result, exit_reason, pnl_points AS pnl,
               time_signal, date
        FROM signals
        WHERE result IS NOT NULL
        ORDER BY date ASC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


# ── Aggregate stats ───────────────────────────────────────────────────────────

def _aggregate(trades: list[dict]) -> dict:
    total   = len(trades)
    wins    = sum(1 for t in trades if t["result"] == "win")
    losses  = sum(1 for t in trades if t["result"] == "loss")
    win_rate = round(wins / total * 100, 1) if total else 0

    # By zone type
    by_type: dict[str, dict] = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    for t in trades:
        k = f"{t['zone_class'].upper()} {t['zone_type']}"
        by_type[k]["wins"]   += 1 if t["result"] == "win" else 0
        by_type[k]["losses"] += 1 if t["result"] == "loss" else 0
        by_type[k]["pnl"]    += t["pnl"] or 0

    type_summary = []
    for k, v in sorted(by_type.items()):
        n = v["wins"] + v["losses"]
        wr = round(v["wins"] / n * 100) if n else 0
        type_summary.append(
            f"  {k}: {n} trades, {wr}% WR, {v['pnl']:+.1f}pts PnL"
        )

    # By exit reason
    by_exit: dict[str, int] = defaultdict(int)
    for t in trades:
        by_exit[t["exit_reason"] or "unknown"] += 1

    exit_summary = [f"  {k}: {v}" for k, v in sorted(by_exit.items(), key=lambda x: -x[1])]

    # By time of day (hour of entry)
    by_hour: dict[int, dict] = defaultdict(lambda: {"wins": 0, "losses": 0})
    for t in trades:
        try:
            h = int((t["time_signal"] or "09:15:00")[:2])
            by_hour[h]["wins"]   += 1 if t["result"] == "win" else 0
            by_hour[h]["losses"] += 1 if t["result"] == "loss" else 0
        except Exception:
            pass

    hour_summary = []
    for h in sorted(by_hour):
        v  = by_hour[h]
        n  = v["wins"] + v["losses"]
        wr = round(v["wins"] / n * 100) if n else 0
        hour_summary.append(f"  {h:02d}:xx  {n} trades  {wr}% WR")

    # By zone class
    demand_trades = [t for t in trades if t["zone_class"] == "demand"]
    supply_trades = [t for t in trades if t["zone_class"] == "supply"]
    demand_wr = round(sum(1 for t in demand_trades if t["result"] == "win") / len(demand_trades) * 100) if demand_trades else 0
    supply_wr = round(sum(1 for t in supply_trades if t["result"] == "win") / len(supply_trades) * 100) if supply_trades else 0

    return {
        "summary":      f"{total} trades | {wins}W {losses}L | {win_rate}% WR",
        "by_type":      "\n".join(type_summary) or "  (none)",
        "by_exit":      "\n".join(exit_summary)  or "  (none)",
        "by_hour":      "\n".join(hour_summary)  or "  (none)",
        "demand_wr":    f"{len(demand_trades)} trades, {demand_wr}% WR",
        "supply_wr":    f"{len(supply_trades)} trades, {supply_wr}% WR",
    }


# ── Read knowledge file ───────────────────────────────────────────────────────

def _load_knowledge() -> str:
    if _KNOWLEDGE.exists():
        text = _KNOWLEDGE.read_text(encoding="utf-8")
        # Trim if very long — keep first 4000 chars
        if len(text) > 4000:
            text = text[:4000] + "\n...[truncated]"
        return text
    return "(AGENT_KNOWLEDGE.md not found — skipping)"


# ── Build prompt ──────────────────────────────────────────────────────────────

def _build_prompt(agg: dict, knowledge: str, current_memory: dict) -> str:
    today = datetime.today().strftime("%Y-%m-%d")
    return f"""You are seeding the long-term memory for a NIFTY intraday demand/supply zone options trading agent.

HISTORICAL TRADE DATA (all closed trades in trades.db):
Overall: {agg['summary']}

By zone type:
{agg['by_type']}

By exit reason:
{agg['by_exit']}

By time of day (hour):
{agg['by_hour']}

Demand zones: {agg['demand_wr']}
Supply zones:  {agg['supply_wr']}

TRADER'S WRITTEN KNOWLEDGE (AGENT_KNOWLEDGE.md):
{knowledge}

CURRENT MEMORY (to merge into, not replace blindly):
{json.dumps(current_memory, indent=2)}

Your task: produce a rich, updated memory.json that captures the real patterns from the data above.

Guidelines:
1. Set market_regime based on overall win rate: >55% = "bullish_edge", 45-55% = "normal", <45% = "choppy"
2. Set departure_thresholds.min and .preferred based on what you can infer from zone quality
3. Set time_of_day_rules based on the hourly win rate data — identify good and bad windows
4. Populate mistake_log with up to 5 real patterns from losing trades (e.g. "DBD supply losses cluster at 09:xx — morning supply often fails")
5. Populate win_patterns with up to 5 real patterns from winning trades
6. Set caution_flags (max 5) with the most important active warnings
7. Set zone_type_notes with a short note per zone type that had meaningful data
8. Set last_trained to today: {today}
9. IMPORTANT: be specific — use the actual numbers from the data, not generic advice

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

    # Load data
    trades = _load_all_trades()
    logger.info("Loaded %d closed trades from trades.db", len(trades))
    if not trades:
        logger.error("No closed trades found — nothing to seed from")
        return

    agg       = _aggregate(trades)
    knowledge = _load_knowledge()
    logger.info("Knowledge file: %d chars", len(knowledge))

    # Load current memory
    current_memory: dict = {}
    if _MEMORY_PATH.exists():
        try:
            current_memory = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Confirm before overwriting
    print(f"\n{agg['summary']}")
    print(f"This will overwrite: {_MEMORY_PATH}")
    confirm = input("Proceed? [y/N] ").strip().lower()
    if confirm != "y":
        logger.info("Aborted.")
        return

    prompt = _build_prompt(agg, knowledge, current_memory)

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

    _MEMORY_PATH.write_text(json.dumps(updated, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Memory seeded → %s", _MEMORY_PATH)
    logger.info(
        "Regime: %s | Cautions: %d | Mistakes: %d | Win patterns: %d",
        updated.get("market_regime"),
        len(updated.get("caution_flags", [])),
        len(updated.get("mistake_log", [])),
        len(updated.get("win_patterns", [])),
    )
    logger.info("=== Seeder complete — review memory.json before next trading day ===")


if __name__ == "__main__":
    sys.path.insert(0, str(_ROOT))
    try:
        from dotenv import load_dotenv
        load_dotenv(_ROOT / ".env")
    except ImportError:
        pass
    run()
