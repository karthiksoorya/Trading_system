"""
agent/trainer.py — EOD daily trainer.

Reads today's closed trades + current memory.json.
Calls Claude Sonnet to synthesise patterns and update memory.
Run via cron at 16:00 (after market close + EOD export).

Usage:
    py -3.14 agent/trainer.py
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
    from journal.db import get_signals_for_date
    rows = get_signals_for_date(date.today().isoformat())
    return [dict(r) for r in rows if r["result"] is not None]


def _build_prompt(trades: list[dict], memory: dict) -> str:
    if not trades:
        trades_text = "No trades closed today."
    else:
        lines = []
        for t in trades:
            sim = f" | SimOutcome={t['sim_outcome']}" if t.get("sim_outcome") else ""
            lines.append(
                f"  [{t['zone_class'].upper()} {t['zone_type']} {t['timeframe']}]"
                f"  Entry={t['entry']:.2f} SL={t['stop_loss']:.2f}"
                f"  Result={t['result'].upper()} PnL={t.get('pnl', 0):+.1f}pts"
                f"  ExitReason={t.get('exit_reason','?')}{sim}"
            )
        trades_text = "\n".join(lines)

    today = date.today().isoformat()
    return f"""You are the daily trainer for a NIFTY demand/supply zone intraday options trading system.

TODAY'S CLOSED TRADES:
{trades_text}

CURRENT MEMORY:
{json.dumps(memory, indent=2)}

Your task: update the memory JSON based on today's outcomes.

Rules:
1. Only modify these fields: mistake_log, win_patterns, caution_flags, market_regime, departure_thresholds, time_of_day_rules, zone_type_notes, last_trained.
2. mistake_log: add a brief pattern note if a loss is clear. Max 10 items — remove oldest if needed.
3. win_patterns: add a brief pattern note if a win is clear. Max 10 items — remove oldest if needed.
4. caution_flags: set active warnings (e.g. "avoid CE before 11:30 when VIX rising"). Max 5 — replace weakest if full.
5. Set last_trained to today: {today}
6. IMPORTANT: mistakes are hypotheses, NOT permanent rules. Do not permanently block a zone type from 1-2 losses.
7. If today had no clear pattern, only update last_trained and nothing else.

Reply with ONLY the updated JSON object. No markdown, no explanation, no code fences. Raw JSON only."""


def run() -> None:
    logger.info("=== Daily Trainer starting ===")

    try:
        import anthropic
    except ImportError:
        logger.error("anthropic not installed — run: py -3.14 -m pip install anthropic")
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set")
        return

    memory = _load_memory()
    logger.info("Memory loaded. Last trained: %s", memory.get("last_trained", "never"))

    try:
        trades = _load_today_trades()
        logger.info("Today's closed trades: %d", len(trades))
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

    _MEMORY_PATH.write_text(json.dumps(updated, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Memory updated → %s", _MEMORY_PATH)
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
