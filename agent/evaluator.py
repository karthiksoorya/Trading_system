"""
agent/evaluator.py — Per-signal Claude evaluation.

Called by scheduler.py when a zone fires. Returns a verdict:
  TRADE  — signal looks good vs memory rules
  SKIP   — signal conflicts with a learned rule
  REVIEW — borderline; send to Telegram with a caution note

Fails safe: any error returns TRADE so live signals are never silently blocked.
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_MEMORY_PATH = Path(__file__).parent / "memory.json"
_MODEL = "claude-haiku-4-5-20251001"


def _load_memory() -> dict:
    try:
        return json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def evaluate(signal_data: dict, zone_data: dict, vix: float | None = None) -> dict:
    """
    Evaluate a signal against learned memory rules.

    Returns:
        {"verdict": "TRADE" | "SKIP" | "REVIEW", "reason": str}
    """
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed — evaluator returning TRADE")
        return {"verdict": "TRADE", "reason": "evaluator unavailable (no anthropic package)"}

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set — evaluator returning TRADE")
        return {"verdict": "TRADE", "reason": "evaluator unavailable (no API key)"}

    memory = _load_memory()
    if not memory or not memory.get("last_trained"):
        return {"verdict": "TRADE", "reason": "no training yet — trade normally"}

    prompt = _build_prompt(signal_data, zone_data, vix, memory)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return _parse_response(msg.content[0].text.strip())
    except Exception as e:
        logger.warning("Evaluator API call failed — returning TRADE: %s", e)
        return {"verdict": "TRADE", "reason": f"API error: {e}"}


def _build_prompt(signal: dict, zone: dict, vix: float | None, memory: dict) -> str:
    cautions     = memory.get("caution_flags", [])
    mistakes     = memory.get("mistake_log", [])[-3:]
    win_patterns = memory.get("win_patterns", [])[-3:]
    dep_min      = memory.get("departure_thresholds", {}).get("min", 1.0)
    dep_pref     = memory.get("departure_thresholds", {}).get("preferred", 1.5)
    tod_avoid    = memory.get("time_of_day_rules", {}).get("avoid_after", "13:00")

    return f"""You are evaluating a NIFTY options signal against learned trading rules.

SIGNAL:
  Zone: {signal.get('zone_class','?').upper()} {signal.get('zone_type','?')} on {signal.get('timeframe','?')}
  Entry: {signal.get('entry',0):.2f} | SL: {signal.get('stop_loss',0):.2f} | Target: {signal.get('intraday_target',0):.2f}
  Score: {signal.get('score',0)} | Confluence: {signal.get('confluence','')}
  ATR departure: {zone.get('departure_strength',0):.2f}x | Base compression: {zone.get('base_compression',0):.2f}x
  VIX: {vix if vix else 'unknown'}

REGIME: {memory.get('market_regime','normal')}

CAUTION FLAGS:
{chr(10).join(f'- {c}' for c in cautions) if cautions else '- none'}

RECENT MISTAKES TO AVOID:
{chr(10).join(f'- {m}' for m in mistakes) if mistakes else '- none'}

WIN PATTERNS TO FAVOUR:
{chr(10).join(f'- {w}' for w in win_patterns) if win_patterns else '- none'}

THRESHOLDS: min departure={dep_min}x, preferred={dep_pref}x, avoid_after={tod_avoid}

Reply with EXACTLY one of these formats (nothing else):
TRADE: <one short reason>
SKIP: <one short reason>
REVIEW: <one short reason>"""


def _parse_response(raw: str) -> dict:
    for verdict in ("TRADE", "SKIP", "REVIEW"):
        if raw.upper().startswith(verdict):
            reason = raw[len(verdict):].lstrip(": ").strip()
            return {"verdict": verdict, "reason": reason}
    logger.warning("Evaluator: unexpected response %r — defaulting to TRADE", raw)
    return {"verdict": "TRADE", "reason": f"unparsed: {raw[:60]}"}
