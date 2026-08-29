"""
agent/ingest.py — Ingest external knowledge sources into agent reference memory.

Reads a source (text file, PDF, markdown, URL) and asks Claude to extract
trading-specific insights, storing them in agent/knowledge/ as reference.
The trainer and seeder automatically pick these up as REFERENCE knowledge.

Usage:
    python3 agent/ingest.py --file "seiden_method.pdf"   --label "Sam Seiden"
    python3 agent/ingest.py --file "my_notes.md"         --label "My Rules"
    python3 agent/ingest.py --file "ict_concepts.txt"    --label "ICT Concepts"
    python3 agent/ingest.py --url  "https://..."         --label "Article"
    python3 agent/ingest.py --list                       (show all ingested sources)
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
_MAX_CHARS  = 12_000   # max chars sent to Claude per source (cost control)

_KB_DIR.mkdir(exist_ok=True)


# ── Slug ─────────────────────────────────────────────────────────────────────

def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


# ── Extract text from different sources ──────────────────────────────────────

def _read_file(path: str) -> str:
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
            return text
        except ImportError:
            try:
                import pypdf
                reader = pypdf.PdfReader(str(p))
                return "\n".join(page.extract_text() or "" for page in reader.pages)
            except ImportError:
                raise ImportError(
                    "PDF support needs pdfplumber or pypdf.\n"
                    "Install: pip install pdfplumber"
                )

    # Plain text, markdown, etc.
    return p.read_text(encoding="utf-8", errors="ignore")


def _read_url(url: str) -> str:
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
        return "\n".join(parser._parts)
    except Exception as e:
        raise RuntimeError(f"Could not fetch URL: {e}")


# ── Build Claude prompt ───────────────────────────────────────────────────────

def _build_prompt(label: str, source_type: str, content: str) -> str:
    truncated = content[:_MAX_CHARS]
    if len(content) > _MAX_CHARS:
        truncated += f"\n...[truncated — {len(content) - _MAX_CHARS} chars omitted]"

    return f"""You are extracting trading knowledge from an external source to train a NIFTY demand/supply zone intraday options trading agent.

SOURCE: "{label}" (type: {source_type})

CONTENT:
{truncated}

Extract ONLY trading-relevant insights specific to:
- Demand/supply zone identification and quality
- Entry rules, confirmation signals, filters
- Exit rules, stop loss, target management
- Time-of-day and market regime patterns
- Risk management principles
- What makes a zone high probability vs low probability

Format your response as JSON with this exact structure:
{{
  "label": "{label}",
  "source_type": "{source_type}",
  "ingested_at": "{datetime.today().strftime('%Y-%m-%d')}",
  "key_concepts": ["concept 1", "concept 2", ...],
  "entry_rules": ["rule 1", "rule 2", ...],
  "exit_rules": ["rule 1", "rule 2", ...],
  "zone_quality_filters": ["filter 1", "filter 2", ...],
  "risk_rules": ["rule 1", "rule 2", ...],
  "cautions": ["what to avoid 1", "what to avoid 2", ...],
  "summary": "2-3 sentence summary of the most important trading insight from this source"
}}

Be specific and actionable. Skip generic advice. If a section has no relevant content, use an empty list [].
Reply with ONLY the JSON object. No markdown fences."""


# ── Ingest a source ───────────────────────────────────────────────────────────

def ingest(label: str, content: str, source_type: str) -> None:
    try:
        import anthropic
    except ImportError:
        logger.error("anthropic not installed — run: pip install anthropic")
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set")
        return

    slug     = _slug(label)
    out_path = _KB_DIR / f"{slug}.json"

    if out_path.exists():
        confirm = input(f"'{label}' already exists. Overwrite? [y/N] ").strip().lower()
        if confirm != "y":
            logger.info("Skipped.")
            return

    logger.info("Sending to Claude Sonnet (%d chars) ...", min(len(content), _MAX_CHARS))
    prompt = _build_prompt(label, source_type, content)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=_MODEL,
            max_tokens=1500,
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

    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Saved → %s", out_path)

    # Print summary
    print(f"\n✓ Ingested: {label}")
    print(f"  Key concepts : {len(data.get('key_concepts', []))}")
    print(f"  Entry rules  : {len(data.get('entry_rules', []))}")
    print(f"  Zone filters : {len(data.get('zone_quality_filters', []))}")
    print(f"  Cautions     : {len(data.get('cautions', []))}")
    print(f"  Summary: {data.get('summary', '')}\n")


# ── List / Remove ─────────────────────────────────────────────────────────────

def list_sources() -> None:
    files = sorted(_KB_DIR.glob("*.json"))
    if not files:
        print("No sources ingested yet.")
        return
    print(f"\n{'Label':<30} {'Ingested':<12} {'Concepts':>8} {'Rules':>6}")
    print("-" * 62)
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            print(
                f"{d.get('label','?'):<30} "
                f"{d.get('ingested_at','?'):<12} "
                f"{len(d.get('key_concepts',[]) + d.get('zone_quality_filters',[])):>8} "
                f"{len(d.get('entry_rules',[]) + d.get('exit_rules',[])):>6}"
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
    """Return a formatted string of all ingested knowledge for use in prompts."""
    files = sorted(_KB_DIR.glob("*.json"))
    if not files:
        return "(no external knowledge ingested yet)"

    parts = []
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            label   = d.get("label", f.stem)
            summary = d.get("summary", "")
            rules   = (
                d.get("key_concepts", []) +
                d.get("entry_rules", []) +
                d.get("zone_quality_filters", []) +
                d.get("exit_rules", []) +
                d.get("risk_rules", []) +
                d.get("cautions", [])
            )
            block = f"[{label}]\n"
            if summary:
                block += f"  {summary}\n"
            for r in rules[:12]:   # max 12 points per source to control prompt size
                block += f"  • {r}\n"
            parts.append(block)
        except Exception:
            pass

    return "\n".join(parts)


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
        content     = _read_file(args.file)
        source_type = Path(args.file).suffix.lstrip(".") or "text"
    elif args.url:
        logger.info("Fetching URL: %s", args.url)
        content     = _read_url(args.url)
        source_type = "url"
    else:
        content     = args.text
        source_type = "text"

    logger.info("Extracted %d chars", len(content))
    ingest(args.label, content, source_type)


if __name__ == "__main__":
    sys.path.insert(0, str(_ROOT))
    try:
        from dotenv import load_dotenv
        load_dotenv(_ROOT / ".env")
    except ImportError:
        pass
    main()
