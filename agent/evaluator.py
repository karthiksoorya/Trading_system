"""
agent/evaluator.py — Per-signal Claude evaluation.

Called by scheduler.py when a zone fires. Returns a verdict:
  TRADE  — signal looks good vs memory rules
  SKIP   — signal conflicts with a learned rule
  REVIEW — borderline; send to Telegram with a caution note

Failure policy: any infrastructure failure (no API key, package missing,
API down, unparseable response) returns REVIEW — never silently TRADE.
The only exception is "no training yet", which correctly passes through.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_MEMORY_PATH = Path(__file__).parent / "memory.json"
_EVAL_LOG    = Path(__file__).parent / "eval_log.jsonl"
_MODEL       = "claude-haiku-4-5-20251001"
_MEMORY_VER  = 1


def _load_memory() -> dict:
    try:
        return json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _audit(signal_data: dict, result: dict, prompt: str | None, raw_response: str | None) -> None:
    """Append one line to eval_log.jsonl for full auditability."""
    try:
        entry = {
            "ts":             datetime.now().isoformat(timespec="seconds"),
            "model":          _MODEL,
            "memory_version": _MEMORY_VER,
            "signal_key":     f"{signal_data.get('zone_class','?')}_{signal_data.get('zone_type','?')}_{signal_data.get('timeframe','?')}",
            "signal_time":    signal_data.get("time_signal", "?"),
            "verdict":        result["verdict"],
            "reason":         result["reason"],
            "prompt_chars":   len(prompt) if prompt else 0,
            "raw_response":   (raw_response or "")[:200],
        }
        with _EVAL_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.debug("Audit log failed (non-critical): %s", e)


def evaluate(signal_data: dict, zone_data: dict, vix: float | None = None) -> dict:
    """
    Evaluate a signal against learned memory rules.

    Returns:
        {"verdict": "TRADE" | "SKIP" | "REVIEW", "reason": str}
    """
    try:
        import anthropic
    except ImportError:
        result = {"verdict": "REVIEW", "reason": "evaluator unavailable — anthropic package not installed"}
        logger.warning("anthropic package not installed — evaluator returning REVIEW")
        _audit(signal_data, result, None, None)
        return result

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        result = {"verdict": "REVIEW", "reason": "evaluator unavailable — ANTHROPIC_API_KEY not set"}
        logger.warning("ANTHROPIC_API_KEY not set — evaluator returning REVIEW")
        _audit(signal_data, result, None, None)
        return result

    memory = _load_memory()
    if not memory or not memory.get("last_trained"):
        result = {"verdict": "TRADE", "reason": "no training yet — trade normally"}
        _audit(signal_data, result, None, None)
        return result

    prompt = _build_prompt(signal_data, zone_data, vix, memory)

    raw = None
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        result = _parse_response(raw)
    except Exception as e:
        result = {"verdict": "REVIEW", "reason": f"API error — check signal manually: {str(e)[:80]}"}
        logger.warning("Evaluator API call failed — returning REVIEW: %s", e)

    _audit(signal_data, result, prompt, raw)
    return result


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
    logger.warning("Evaluator: unexpected response %r — defaulting to REVIEW", raw)
    return {"verdict": "REVIEW", "reason": f"unparsed response — check manually: {raw[:60]}"}
