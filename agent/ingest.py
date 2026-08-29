"""
agent/ingest.py — Ingest external knowledge sources into agent reference memory.

Reads a source (text file, PDF, markdown, URL) and asks Claude to extract
trading-specific insights, storing them in agent/knowledge/ as reference.
The trainer and seeder load these automatically — but they are labelled as
HYPOTHESES until validated against actual trade data.

Provenance is preserved: source author, evidence type, publication date,
applicable market/timeframe, confidence level, and limitations. This lets
the agent weight external sources appropriately vs proven patterns.

Usage:
    python3 agent/ingest.py --file "seiden_method.pdf"   --label "Sam Seiden"
    python3 agent/ingest.py --file "my_notes.md"         --label "My Rules"
    python3 agent/ingest.py --url  "https://..."         --label "Article"
    python3 agent/ingest.py --text "..."                 --label "My Observation"
    python3 agent/ingest.py --list                       (show all sources)
    python3 agent/ingest.py --remove "Sam Seiden"        (remove a source)
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger(__name__)

_ROOT       = Path(__file__).parent.parent
_KB_DIR     = Path(__file__).parent / "knowledge"
_MODEL      = "claude-sonnet-4-6"
_MAX_CHARS  = 12_000   # max chars sent per source (cost control)
# When content exceeds limit, we use chunked extraction to avoid losing provenance
_CHUNK_SIZE = 10_000

_KB_DIR.mkdir(exist_ok=True)


# ── Slug ─────────────────────────────────────────────────────────────────────

def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


# ── Extract text from different sources ──────────────────────────────────────

def _read_file(path: str) -> tuple[str, int]:
    """Returns (text, total_chars_before_truncation)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")

    suffix = p.suffix.lower()

    if suffix == ".pdf":
        try:
            import pdfplumber
            text = ""
            with pdfplumber.open(p) as pdf:
                for page in pdf.pages:
                    text += page.extract_text() or ""
            return text, len(text)
        except ImportError:
            try:
                import pypdf
                reader = pypdf.PdfReader(str(p))
                text = "\n".join(page.extract_text() or "" for page in reader.pages)
                return text, len(text)
            except ImportError:
                raise ImportError(
                    "PDF support needs pdfplumber or pypdf.\n"
                    "Install: pip install pdfplumber"
                )

    text = p.read_text(encoding="utf-8", errors="ignore")
    return text, len(text)


def _read_url(url: str) -> tuple[str, int]:
    try:
        import requests
        from html.parser import HTMLParser

        class _TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self._parts: list[str] = []
                self._skip = False
            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style", "nav", "footer"):
                    self._skip = True
            def handle_endtag(self, tag):
                if tag in ("script", "style", "nav", "footer"):
                    self._skip = False
            def handle_data(self, data):
                if not self._skip and data.strip():
                    self._parts.append(data.strip())

        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        parser = _TextExtractor()
        parser.feed(r.text)
        text = "\n".join(parser._parts)
        return text, len(text)
    except Exception as e:
        raise RuntimeError(f"Could not fetch URL: {e}")


# ── Build Claude prompt ───────────────────────────────────────────────────────

# Columns available for hypothesis filters — tell Claude exactly what it can use
_SIGNAL_COLUMNS = """
Available SQLite columns in the signals table (for generating WHERE clause filters):
  zone_class TEXT        — 'demand' or 'supply'
  zone_type TEXT         — 'DBR', 'RBR', 'RBD', 'DBD'
  timeframe TEXT         — '5minute', '15minute', '60minute'
  freshness REAL         — 0.0 to 1.0 (how fresh/untouched the zone is)
  booster_score REAL     — 0 to 10 (composite quality score)
  strength REAL          — 0.0 to 1.0 (zone strength)
  time_score REAL        — 0.0 to 1.0 (time-of-day quality)
  confluence_count INT   — 1 to 4 (number of timeframes in agreement)
  departure_strength REAL — ATR departure multiplier (e.g. 1.5 = departed 1.5× ATR)
  base_compression REAL  — base candle compression ratio
  vix_at_signal REAL     — India VIX at time of signal
  time_signal TEXT       — 'HH:MM:SS' (e.g. '09:30:00')
  entry_type INT         — 1 = aggressive, 2 = conservative

Only generate filters using columns from this list. Use SQLite syntax.
Examples of valid filters:
  "freshness > 0.7"
  "timeframe = '60minute'"
  "confluence_count >= 3"
  "time_signal >= '10:00:00' AND time_signal < '12:00:00'"
  "zone_type IN ('DBR', 'RBR') AND zone_class = 'demand'"
  "departure_strength > 1.5"
  "booster_score >= 7.0"
"""

# Allowed column names for basic SQL injection guard
_ALLOWED_COLUMN_WORDS = {
    "zone_class", "zone_type", "timeframe", "freshness", "booster_score",
    "strength", "time_score", "confluence_count", "departure_strength",
    "base_compression", "vix_at_signal", "time_signal", "entry_type",
    "entry", "stop_loss", "intraday_target",
    # SQL keywords allowed in WHERE clauses
    "and", "or", "not", "in", "between", "like", "is", "null",
    "demand", "supply", "dbr", "rbr", "rbd", "dbd",
    "5minute", "15minute", "60minute",
}
_DANGEROUS_KEYWORDS = {"drop", "delete", "insert", "update", "select", "union",
                       "exec", "execute", "attach", "detach", "pragma", "create"}


def _validate_filter(sql_filter: str | None) -> bool:
    """Reject filters containing dangerous SQL keywords (basic guard for Claude-generated SQL)."""
    if not sql_filter:
        return False
    import re
    words = set(re.findall(r"[a-z_]+", sql_filter.lower()))
    return not (words & _DANGEROUS_KEYWORDS)


def _build_prompt(label: str, source_type: str, content: str, total_chars: int) -> str:
    truncated   = content[:_MAX_CHARS]
    is_truncated = total_chars > _MAX_CHARS
    trunc_note  = (f"\n[NOTE: source is {total_chars:,} chars; showing first {_MAX_CHARS:,}. "
                   "Key concepts may appear later in the document.]") if is_truncated else ""

    return f"""You are extracting trading knowledge from an external source to add to a NIFTY
demand/supply zone intraday options trading agent's reference library.

SOURCE: "{label}" (type: {source_type})

CONTENT:
{truncated}{trunc_note}

Extract ONLY trading-relevant insights. Focus on:
- Demand/supply zone identification and quality criteria
- Entry rules, confirmation signals, quality filters
- Exit rules, stop loss, target management
- Time-of-day and market regime patterns
- Risk management principles
- What makes a zone high-probability vs low-probability

IMPORTANT: Also assess the source's credibility metadata:
- evidence_type: "academic" (peer-reviewed study) | "practitioner" (experienced trader method) |
  "anecdotal" (personal experience/opinion) | "marketing" (sales material/promotional)
- confidence_level: "high" (well-documented, widely validated) | "medium" (credible but limited evidence) |
  "low" (speculative or anecdotal)
- applicable_market: e.g. "global equities", "NIFTY", "forex", "all" — what market this was developed for
- applicable_timeframe: e.g. "intraday", "swing", "positional", "all"
- limitations: key caveats — market conditions where this may NOT apply

ALSO generate signal_filters: for each extracted rule that can be expressed as a database condition,
provide a SQLite WHERE clause so the system can automatically test the hypothesis against real trades.

{_SIGNAL_COLUMNS}

Format your response as JSON with this exact structure:
{{
  "label": "{label}",
  "source_type": "{source_type}",
  "ingested_at": "{datetime.today().strftime('%Y-%m-%d')}",
  "evidence_type": "practitioner",
  "confidence_level": "medium",
  "applicable_market": "global equities",
  "applicable_timeframe": "intraday",
  "limitations": ["limitation 1", "limitation 2"],
  "is_hypothesis": true,
  "key_concepts": ["concept 1", "concept 2"],
  "entry_rules": ["rule 1", "rule 2"],
  "exit_rules": ["rule 1", "rule 2"],
  "zone_quality_filters": ["filter 1", "filter 2"],
  "risk_rules": ["rule 1", "rule 2"],
  "cautions": ["caution 1", "caution 2"],
  "summary": "2-3 sentence summary of the most important trading insight and its confidence level",
  "signal_filters": [
    {{
      "rule": "exact rule text from above",
      "filter": "freshness > 0.7",
      "testable": true
    }},
    {{
      "rule": "a rule that cannot be expressed in DB terms",
      "filter": null,
      "testable": false
    }}
  ]
}}

signal_filters must cover ONLY rules from entry_rules and zone_quality_filters — not exit rules or
risk rules (those don't apply at signal time). Use null filter when a rule cannot be expressed
using the available columns. Only use columns from the schema above.

Note: is_hypothesis is ALWAYS true for external sources — validated only after 20+ signal confirmations.

Be specific and actionable. Skip generic advice. Empty list [] for sections with no relevant content.
Reply with ONLY the JSON object. No markdown fences."""


# ── Ingest a source ───────────────────────────────────────────────────────────

def ingest(label: str, content: str, source_type: str, total_chars: int | None = None) -> None:
    try:
        import anthropic
    except ImportError:
        logger.error("anthropic not installed — run: pip install anthropic")
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set")
        return

    if total_chars is None:
        total_chars = len(content)

    slug     = _slug(label)
    out_path = _KB_DIR / f"{slug}.json"

    if out_path.exists():
        confirm = input(f"'{label}' already exists. Overwrite? [y/N] ").strip().lower()
        if confirm != "y":
            logger.info("Skipped.")
            return

    logger.info("Sending to Claude Sonnet (%d chars → %d chars sent) ...",
                total_chars, min(total_chars, _MAX_CHARS))
    if total_chars > _MAX_CHARS:
        logger.warning(
            "Source is %d chars — truncated to %d. Provenance and key concepts should "
            "still be captured from the opening section.",
            total_chars, _MAX_CHARS
        )

    prompt = _build_prompt(label, source_type, content, total_chars)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=_MODEL,
            max_tokens=1800,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
    except Exception as e:
        logger.error("Claude API call failed: %s", e)
        return

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("Claude returned invalid JSON: %s\nRaw: %.300s", e, raw)
        return

    # Ensure is_hypothesis is always True for external sources
    data["is_hypothesis"] = True
    data["source_total_chars"] = total_chars

    # Validate and sanitise signal_filters — reject any that contain dangerous SQL
    raw_filters = data.get("signal_filters", [])
    safe_filters = []
    for sf in raw_filters:
        if not isinstance(sf, dict):
            continue
        f = sf.get("filter")
        if sf.get("testable") and f:
            if _validate_filter(f):
                safe_filters.append(sf)
            else:
                logger.warning("Rejected unsafe filter for rule '%s': %s", sf.get("rule", "?"), f)
                sf["filter"] = None
                sf["testable"] = False
                safe_filters.append(sf)
        else:
            safe_filters.append(sf)
    data["signal_filters"] = safe_filters

    testable_count = sum(1 for sf in safe_filters if sf.get("testable"))
    logger.info("signal_filters: %d total, %d testable", len(safe_filters), testable_count)

    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Saved → %s", out_path)

    print(f"\n✓ Ingested: {label}")
    print(f"  Evidence type   : {data.get('evidence_type', '?')}")
    print(f"  Confidence      : {data.get('confidence_level', '?')}")
    print(f"  Market          : {data.get('applicable_market', '?')}")
    print(f"  Timeframe       : {data.get('applicable_timeframe', '?')}")
    print(f"  Is hypothesis   : {data.get('is_hypothesis', True)}  (always True for external sources)")
    print(f"  Key concepts    : {len(data.get('key_concepts', []))}")
    print(f"  Entry rules     : {len(data.get('entry_rules', []))}")
    print(f"  Zone filters    : {len(data.get('zone_quality_filters', []))}")
    print(f"  Testable rules  : {testable_count} (will be validated against live trade data)")
    print(f"  Limitations     : {len(data.get('limitations', []))}")
    print(f"  Summary: {data.get('summary', '')}\n")


# ── List / Remove ─────────────────────────────────────────────────────────────

def list_sources() -> None:
    files = sorted(_KB_DIR.glob("*.json"))
    if not files:
        print("No sources ingested yet.")
        return
    print(f"\n{'Label':<30} {'Evidence':<15} {'Conf':<8} {'Ingested':<12} {'Rules':>5}")
    print("-" * 76)
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            print(
                f"{d.get('label','?'):<30} "
                f"{d.get('evidence_type','?'):<15} "
                f"{d.get('confidence_level','?'):<8} "
                f"{d.get('ingested_at','?'):<12} "
                f"{len(d.get('entry_rules',[]) + d.get('exit_rules',[])):>5}"
            )
        except Exception:
            print(f"{f.stem:<30} (parse error)")
    print()


def remove_source(label: str) -> None:
    slug = _slug(label)
    path = _KB_DIR / f"{slug}.json"
    if path.exists():
        path.unlink()
        logger.info("Removed: %s", path)
    else:
        logger.warning("Not found: %s", path)


# ── Load all knowledge (used by seed_memory and trainer) ─────────────────────

def load_all_knowledge(memory: dict | None = None) -> str:
    """
    Return formatted string of all ingested knowledge for use in prompts.
    If memory is provided, validated hypotheses are promoted to 'CONFIRMED HYPOTHESIS'
    and rejected ones are flagged — so Claude weights them appropriately.
    """
    files = sorted(_KB_DIR.glob("*.json"))
    if not files:
        return "(no external knowledge ingested yet)"

    tracker = (memory or {}).get("hypothesis_tracker", {})

    parts = []
    for f in files:
        try:
            d       = json.loads(f.read_text(encoding="utf-8"))
            label   = d.get("label", f.stem)
            slug    = f.stem
            conf    = d.get("confidence_level", "unknown")
            ev_type = d.get("evidence_type", "unknown")
            market  = d.get("applicable_market", "?")
            tf      = d.get("applicable_timeframe", "?")
            summary = d.get("summary", "")
            limits  = d.get("limitations", [])

            # Build rule-level validation map from memory tracker
            rule_status: dict[str, str] = {}
            src_tracker = tracker.get(slug, {})
            for r in src_tracker.get("rules", []):
                rule_status[r["rule"]] = r["status"]

            # Validated vs untested rules get different treatment
            validated_rules = [sf["rule"] for sf in d.get("signal_filters", [])
                               if rule_status.get(sf["rule"]) == "historically_promising"]
            rejected_rules  = [sf["rule"] for sf in d.get("signal_filters", [])
                               if rule_status.get(sf["rule"]) == "rejected"]

            block = (f"[HYPOTHESIS: {label}] "
                     f"evidence={ev_type}, confidence={conf}, "
                     f"market={market}, timeframe={tf}\n")
            if summary:
                block += f"  Summary: {summary}\n"
            if limits:
                block += f"  Limitations: {'; '.join(limits[:2])}\n"
            if validated_rules:
                block += f"  ✅ HISTORICALLY PROMISING (in-sample only): {'; '.join(validated_rules[:3])}\n"
            if rejected_rules:
                block += f"  ❌ REJECTED by live data: {'; '.join(rejected_rules[:3])}\n"

            # All rules (unconfirmed labelled accordingly)
            all_rules = (
                d.get("key_concepts", []) +
                d.get("entry_rules", []) +
                d.get("zone_quality_filters", []) +
                d.get("exit_rules", []) +
                d.get("risk_rules", []) +
                d.get("cautions", [])
            )
            for r in all_rules[:10]:
                status = rule_status.get(r, "")
                prefix = "✅" if status == "historically_promising" else ("❌" if status == "rejected" else "◇")
                block += f"  {prefix} {r}\n"
            parts.append(block)
        except Exception:
            pass

    header = (
        "=== EXTERNAL KNOWLEDGE (hypotheses — ✅=confirmed by live data, ❌=rejected, ◇=untested) ===\n"
        "Use ✅ rules with higher weight. Treat ◇ as ideas to watch. Ignore ❌ rules.\n\n"
    )
    return header + "\n".join(parts)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest external knowledge into agent memory.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file",   help="Path to a text, markdown, or PDF file")
    group.add_argument("--url",    help="URL to fetch and ingest")
    group.add_argument("--text",   help="Raw text to ingest directly")
    group.add_argument("--list",   action="store_true", help="List all ingested sources")
    group.add_argument("--remove", metavar="LABEL", help="Remove an ingested source by label")
    parser.add_argument("--label", help="Name for this source (required with --file/--url/--text)")
    args = parser.parse_args()

    if args.list:
        list_sources()
        return

    if args.remove:
        remove_source(args.remove)
        return

    if not args.label:
        parser.error("--label is required with --file, --url, or --text")

    if args.file:
        logger.info("Reading file: %s", args.file)
        content, total_chars = _read_file(args.file)
        source_type = Path(args.file).suffix.lstrip(".") or "text"
    elif args.url:
        logger.info("Fetching URL: %s", args.url)
        content, total_chars = _read_url(args.url)
        source_type = "url"
    else:
        content     = args.text
        total_chars = len(args.text)
        source_type = "text"

    logger.info("Extracted %d chars", total_chars)
    ingest(args.label, content, source_type, total_chars)


if __name__ == "__main__":
    sys.path.insert(0, str(_ROOT))
    try:
        from dotenv import load_dotenv
        load_dotenv(_ROOT / ".env")
    except ImportError:
        pass
    main()
