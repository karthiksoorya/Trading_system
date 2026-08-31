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
    if r == "continuation":
        return "continuation"   # zone was false — breakout consumed the order
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
               departure_strength, base_compression, vix_at_signal, iv_rank_at_signal,
               exit_reason, time_signal, date
        FROM signals
        WHERE result IS NOT NULL OR sim_outcome IS NOT NULL
        ORDER BY date ASC
    """)
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


# ── Aggregate stats ───────────────────────────────────────────────────────────

def _aggregate(trades: list[dict]) -> dict:
    actual    = [t for t in trades if t["data_type"] == "actual"]
    simulated = [t for t in trades if t["data_type"] == "simulated"]
    total     = len(trades)

    def _stats(rows):
        wins         = sum(1 for t in rows if _classify(t["effective_result"]) == "win")
        losses       = sum(1 for t in rows if _classify(t["effective_result"]) == "loss")
        continuations= sum(1 for t in rows if _classify(t["effective_result"]) == "continuation")
        neutral      = len(rows) - wins - losses - continuations
        wr           = round(wins / (wins + losses) * 100, 1) if (wins + losses) else 0
        return wins, losses, continuations, neutral, wr

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
        total_opts_pnl   = sum(t["net_options_pnl_rs"] for t in opts_trades)
        total_gross_pnl  = sum(
            (t.get("options_exit_price", 0) - t.get("options_entry_price", 0))
            * (t.get("options_lot_size") or 0)
            for t in opts_trades
        )
        total_costs      = sum(t.get("execution_cost_rs", 0) or 0 for t in opts_trades)
        opts_summary     = (
            f"  {len(opts_trades)} trades with options data | "
            f"Gross ₹{total_gross_pnl:+,.0f} | "
            f"Costs ₹{total_costs:,.0f} | "
            f"Net ₹{total_opts_pnl:+,.0f}"
        )
    else:
        opts_summary = "  (no options P&L data available)"

    # By zone class
    demand_trades = [t for t in trades if t["zone_class"] == "demand"]
    supply_trades = [t for t in trades if t["zone_class"] == "supply"]

    a_wins, a_losses, a_cont, a_neutral, a_wr = _stats(actual)
    s_wins, s_losses, s_cont, s_neutral, s_wr = _stats(simulated)

    # Zone feature statistics (departure_strength, base_compression, vix_at_signal)
    def _pct(vals, p):
        s = sorted(v for v in vals if v is not None)
        if not s: return None
        i = int(len(s) * p / 100)
        return round(s[min(i, len(s)-1)], 2)

    deps  = [t.get("departure_strength") for t in trades]
    comps = [t.get("base_compression")   for t in trades]
    vixs  = [t.get("vix_at_signal")      for t in trades]
    ivrs  = [t.get("iv_rank_at_signal")  for t in trades]

    def _feat_summary(vals, label, unit=""):
        filled = [v for v in vals if v is not None]
        if not filled:
            return f"  {label}: no data recorded yet"
        return (f"  {label}: n={len(filled)}, "
                f"p25={_pct(vals,25)}{unit}, median={_pct(vals,50)}{unit}, "
                f"p75={_pct(vals,75)}{unit}, max={_pct(vals,100)}{unit}")

    # Win vs loss feature medians (for threshold guidance)
    win_deps  = [t.get("departure_strength") for t in trades if _classify(t["effective_result"])=="win"  and t.get("departure_strength")]
    loss_deps = [t.get("departure_strength") for t in trades if _classify(t["effective_result"])=="loss" and t.get("departure_strength")]
    dep_insight = ""
    if win_deps and loss_deps:
        dep_insight = (f"\n  Departure WR insight: winners median={_pct(win_deps,50)}x, "
                       f"losers median={_pct(loss_deps,50)}x")

    zone_features = (
        _feat_summary(deps,  "Departure strength", "x") + dep_insight + "\n" +
        _feat_summary(comps, "Base compression",   "x") + "\n" +
        _feat_summary(vixs,  "VIX at signal") + "\n" +
        _feat_summary(ivrs,  "IV Rank at signal", "%")
    )

    return {
        "summary":       (f"{total} total | "
                          f"{len(actual)} actual ({a_wins}W/{a_losses}L/{a_cont}C/{a_neutral}N, {a_wr}% WR) | "
                          f"{len(simulated)} simulated ({s_wins}W/{s_losses}L/{s_cont}C/{s_neutral}N, {s_wr}% WR) | "
                          f"C=continuation (false zone/breakout)"),
        "by_type":       "\n".join(type_summary) or "  (none)",
        "by_exit":       "\n".join(exit_summary)  or "  (none)",
        "by_hour":       "\n".join(hour_summary)  or "  (none)",
        "opts_pnl":      opts_summary,
        "zone_features": zone_features,
        "demand_wr":     (f"{len(demand_trades)} signals, "
                          f"{_stats(demand_trades)[4]}% WR (wins vs decided)"),
        "supply_wr":     (f"{len(supply_trades)} signals, "
                          f"{_stats(supply_trades)[4]}% WR (wins vs decided)"),
        "actual_ct":     len(actual),
        "sim_ct":        len(simulated),
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
DATA QUALITY WARNING: Simulated outcomes assume the signal's entry price was fillable — there is no confirmation
that the market traded at that price after the signal fired. Simulated results may be optimistically biased.
Treat simulated win rates as directional indicators, NOT as ground truth. Weight actual trades more heavily.
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

Zone features (departure_strength = how far price moved from zone before signal; base_compression = how tight the base was):
{agg['zone_features']}

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


# ── Hypothesis tracker seeding from full history ──────────────────────────────

def _seed_hypothesis_tracker() -> dict:
    """
    Run all testable hypothesis filters against the full historical DB.
    Returns a fully-populated hypothesis_tracker dict.
    """
    kb_dir = Path(__file__).parent / "knowledge"
    tracker: dict = {}

    if not _DB_PATH.exists():
        return tracker

    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row

    # Baseline WR across all historical data
    baseline = conn.execute("""
        SELECT
            SUM(CASE WHEN COALESCE(result, sim_outcome) IN ('win','target')    THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN COALESCE(result, sim_outcome) IN ('loss','stoploss') THEN 1 ELSE 0 END) AS losses
        FROM signals
        WHERE result IS NOT NULL OR sim_outcome IS NOT NULL
    """).fetchone()

    b_wins  = baseline["wins"]   or 0
    b_losses = baseline["losses"] or 0
    b_wr    = b_wins / (b_wins + b_losses) if (b_wins + b_losses) else 0.5
    logger.info("Historical baseline WR: %.1f%% (%d wins / %d losses)", b_wr * 100, b_wins, b_losses)

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

        tracker[slug] = {"source": kd.get("label", slug), "rules": []}
        logger.info("Testing %d filters for: %s", len(filters), kd.get("label", slug))

        for sf in filters:
            rule_text  = sf["rule"]
            sql_filter = sf["filter"]
            try:
                row = conn.execute(f"""
                    SELECT
                        SUM(CASE WHEN COALESCE(result, sim_outcome) IN ('win','target')    THEN 1 ELSE 0 END) AS wins,
                        SUM(CASE WHEN COALESCE(result, sim_outcome) IN ('loss','stoploss') THEN 1 ELSE 0 END) AS losses
                    FROM signals
                    WHERE (result IS NOT NULL OR sim_outcome IS NOT NULL)
                      AND ({sql_filter})
                """).fetchone()

                wins   = row["wins"]   or 0
                losses = row["losses"] or 0
                total  = wins + losses
                wr     = wins / total if total else 0

                if total >= 20:
                    if   wr >= b_wr + 0.10:  status = "historically_promising"
                    elif wr <= b_wr - 0.10:  status = "rejected"
                    else:                     status = "inconclusive"
                elif total >= 5:
                    status = "testing"
                else:
                    status = "untested"

                logger.info(
                    "  [%s] %s | %d tested, %d W/%d L, WR=%.0f%% vs baseline %.0f%%",
                    status.upper(), rule_text[:60], total, wins, losses, wr*100, b_wr*100
                )
                tracker[slug]["rules"].append({
                    "rule":           rule_text,
                    "filter":         sql_filter,
                    "signals_tested": total,
                    "wins":           wins,
                    "losses":         losses,
                    "status":         status,
                })
            except Exception as e:
                logger.warning("Filter error for '%s': %s", rule_text[:40], e)

    conn.close()
    return tracker


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

    # Load ingested external knowledge (pass current memory for validation status)
    from agent.ingest import load_all_knowledge
    ext_knowledge = load_all_knowledge(current_memory)
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

    # Seed hypothesis tracker from full historical data
    logger.info("Seeding hypothesis tracker from historical data ...")
    updated["hypothesis_tracker"] = _seed_hypothesis_tracker()

    validated = sum(
        1 for src in updated["hypothesis_tracker"].values()
        for r in src.get("rules", []) if r.get("status") == "historically_promising"
    )
    rejected = sum(
        1 for src in updated["hypothesis_tracker"].values()
        for r in src.get("rules", []) if r.get("status") == "rejected"
    )

    candidate_path.write_text(json.dumps(updated, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Candidate written → %s", candidate_path)
    logger.info(
        "Regime: %s | Cautions: %d | Mistakes: %d | Win patterns: %d | "
        "Hypotheses validated: %d | rejected: %d",
        updated.get("market_regime"),
        len(updated.get("caution_flags", [])),
        len(updated.get("mistake_log", [])),
        len(updated.get("win_patterns", [])),
        validated, rejected,
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
