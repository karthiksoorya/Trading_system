"""
agent/brief.py — Morning brief via Telegram.

Reads memory.json, calls Claude Haiku, sends a short daily context message.
Run via cron at 09:00 (before market open).

Usage:
    py -3.14 agent/brief.py
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
_MODEL       = "claude-haiku-4-5-20251001"


def _load_memory() -> dict:
    if _MEMORY_PATH.exists():
        return json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
    return {}


def _build_prompt(memory: dict) -> str:
    regime       = memory.get("market_regime", "normal")
    cautions     = memory.get("caution_flags", [])
    dep_min      = memory.get("departure_thresholds", {}).get("min", 1.0)
    dep_pref     = memory.get("departure_thresholds", {}).get("preferred", 1.5)
    tod_prefer   = memory.get("time_of_day_rules", {}).get("prefer_before", "11:30")
    tod_avoid    = memory.get("time_of_day_rules", {}).get("avoid_after", "13:00")
    win_patterns = memory.get("win_patterns", [])[-2:]
    last_trained = memory.get("last_trained", "unknown")

    caution_text = "\n".join(f"- {c}" for c in cautions) if cautions else "- none"
    win_text     = "\n".join(f"- {w}" for w in win_patterns) if win_patterns else "- none"

    return f"""You are sending a morning brief for a NIFTY intraday demand/supply zone options trader.
Today: {date.today().strftime("%A %d %b %Y")}
Memory last updated: {last_trained}

ACTIVE RULES:
- Regime: {regime}
- Departure: prefer ≥{dep_pref}x ATR, minimum {dep_min}x ATR
- Time: trade before {tod_prefer}, avoid after {tod_avoid}
- Cautions:
{caution_text}
- Recent win patterns:
{win_text}

Write a brief (4-6 lines max):
1. Today's regime + key caution (if any)
2. Departure threshold to use today
3. Time window
4. One reminder from win patterns (if any)

Direct trading language. No greetings. Start exactly with: 📋 Morning Brief"""


def _apply_regime_settings(memory: dict) -> str:
    """
    Auto-update settings.json based on memory regime before engine starts.
    Returns a summary string of what was changed (for the brief).
    """
    sys.path.insert(0, str(_ROOT))
    import config

    regime   = memory.get("market_regime", "normal").lower()
    cautions = memory.get("caution_flags", [])
    tod      = memory.get("time_of_day_rules", {})

    current  = config.load_settings()
    changes  = {}

    # ── Zone classes based on regime ──────────────────────────────────────
    if "bullish" in regime:
        zone_classes = ["demand", "supply"]   # both, demand favored
    elif "bearish" in regime:
        zone_classes = ["supply"]             # PE only
    else:
        zone_classes = ["demand", "supply"]   # neutral → both

    # Only write if different from current
    if current.get("SCAN_ZONE_CLASSES") != zone_classes:
        changes["SCAN_ZONE_CLASSES"] = zone_classes

    # ── Scan window end time from time_of_day_rules ───────────────────────
    prefer_before = tod.get("prefer_before")
    if prefer_before:
        current_win = current.get("SCAN_WINDOW", {"start": "09:15", "end": "15:00"})
        if current_win.get("end") != prefer_before:
            changes["SCAN_WINDOW"] = {"start": current_win.get("start", "09:15"), "end": prefer_before}

    if changes:
        config.save_settings(changes)
        logger.info("Regime settings applied: %s", changes)
        parts = []
        if "SCAN_ZONE_CLASSES" in changes:
            parts.append(f"zones→{'+'.join(zone_classes)}")
        if "SCAN_WINDOW" in changes:
            parts.append(f"scan_end→{prefer_before}")
        return "⚙️ Auto-configured: " + ", ".join(parts)
    return ""


def run() -> None:
    logger.info("=== Morning Brief starting ===")

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
    if not memory or not memory.get("last_trained"):
        logger.info("No training yet — skipping brief (run trainer.py first)")
        return

    # Apply regime-based settings before engine starts at 09:05
    regime_note = _apply_regime_settings(memory)

    prompt = _build_prompt(memory)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        brief_text = msg.content[0].text.strip()
    except Exception as e:
        logger.error("Claude API call failed: %s", e)
        return

    if regime_note:
        brief_text += f"\n\n{regime_note}"

    logger.info("Brief:\n%s", brief_text)

    sys.path.insert(0, str(_ROOT))
    try:
        import notify
        notify._send(brief_text)
        logger.info("Brief sent to Telegram.")
    except Exception as e:
        logger.error("Telegram send failed: %s", e)

    logger.info("=== Morning Brief complete ===")


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(_ROOT / ".env")
    except ImportError:
        pass
    run()
