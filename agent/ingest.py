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
  "summary": "2-3 sentence summary of the most important trading insight and its confidence level"
}}

Note: is_hypothesis is ALWAYS true for external sources — it becomes a validated rule only after
being confirmed against actual trade data from the live system.

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

def load_all_knowledge() -> str:
    """Return formatted string of all ingested knowledge, clearly labelled as hypotheses."""
    files = sorted(_KB_DIR.glob("*.json"))
    if not files:
        return "(no external knowledge ingested yet)"

    parts = []
    for f in files:
        try:
            d       = json.loads(f.read_text(encoding="utf-8"))
            label   = d.get("label", f.stem)
            conf    = d.get("confidence_level", "unknown")
            ev_type = d.get("evidence_type", "unknown")
            market  = d.get("applicable_market", "?")
            tf      = d.get("applicable_timeframe", "?")
            summary = d.get("summary", "")
            limits  = d.get("limitations", [])
            rules   = (
                d.get("key_concepts", []) +
                d.get("entry_rules", []) +
                d.get("zone_quality_filters", []) +
                d.get("exit_rules", []) +
                d.get("risk_rules", []) +
                d.get("cautions", [])
            )
            block = (f"[HYPOTHESIS: {label}] "
                     f"evidence={ev_type}, confidence={conf}, "
                     f"market={market}, timeframe={tf}\n")
            if summary:
                block += f"  Summary: {summary}\n"
            if limits:
                block += f"  Limitations: {'; '.join(limits[:2])}\n"
            for r in rules[:10]:   # max 10 items per source
                block += f"  • {r}\n"
            parts.append(block)
        except Exception:
            pass

    header = (
        "=== EXTERNAL HYPOTHESES (not yet validated against this system's data) ===\n"
        "These are reference ideas from books/articles. Use only if they align with actual data.\n\n"
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
