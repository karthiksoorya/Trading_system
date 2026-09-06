"""Offline, read-only journal integration for completed-trade observations.

No journal/config imports: journal helpers can commit/migrate and config imports
create directories. The default DB location matches config.DB_PATH without those
side effects. Use one source journal per knowledge store: signal IDs are local to
that journal. Existing reflection IDs are never overwritten or enriched on rerun.

Phase 2 outcomes are gross premium changes, not cost-adjusted profitability or
proof of strategy quality. The frozen classifier's option_pnl slot is used only
for its sign here, with the gross basis recorded explicitly in every observation.
"""

from contextlib import closing
from dataclasses import dataclass
from datetime import date
from datetime import datetime, time
import json
import math
import re
from pathlib import Path
import sqlite3
import sys
from typing import TextIO

from agents.knowledge_store import DEFAULT_PATH, KnowledgeStore
from agents.learning_models import KnowledgeEntry, KnowledgeStatus
from agents.reflection import TradeReflectionInput, classify_trade
from agents.greeks import calculate_greeks


DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data/trades.db"
TRADE_FIELDS = (
    "id", "status", "date", "time_signal", "zone_type", "zone_class", "timeframe",
    "proximal", "distal", "entry", "stop_loss", "intraday_target", "booster_score",
    "freshness", "strength", "time_score", "rr_score", "confluence_count",
    "exit_time", "exit_price", "exit_reason", "pnl_points", "result",
    "options_symbol", "options_entry_price", "options_exit_price", "options_lot_size",
    "mode", "notes", "closed_by",
    "kite_order_id", "vix_at_signal", "option_fill_time", "underlying_at_option_fill",
    "underlying_fill_basis", "signal_to_fill_seconds",
)
MARKET_CLOSE = time(15, 30)


def normalize_trade_date(trade_date: str | None = None) -> str:
    """Use the journal's local-date convention, not the observation's UTC date."""
    if trade_date is None:
        return date.today().isoformat()
    parsed = date.fromisoformat(trade_date)
    if parsed.isoformat() != trade_date:
        raise ValueError("Date must use YYYY-MM-DD")
    return trade_date

def normalize_date_range(from_date=None, to_date=None):
    if (from_date is None) != (to_date is None):
        raise ValueError("from_date and to_date must be supplied together")
    if from_date is None: return None, None
    start, end = normalize_trade_date(from_date), normalize_trade_date(to_date)
    if start > end: raise ValueError("from_date must be <= to_date")
    return start, end


def read_completed_trades(db_path: str | Path = DEFAULT_DB_PATH,
                          trade_date: str | None = None, from_date=None, to_date=None) -> list[dict]:
    """Select only closed signals for the given signal date; never create a DB.

    Missing optional legacy columns are exposed as None without migrations.
    id/status/date are required so selection and identity remain unambiguous.
    """
    if trade_date is not None and (from_date is not None or to_date is not None):
        raise ValueError("--date cannot be combined with a date range")
    selected_date = normalize_trade_date(trade_date) if trade_date is not None else None
    start, end = normalize_date_range(from_date, to_date)
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Journal database does not exist: {path}")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(signals)")}
        missing = {"id", "status", "date"} - columns
        if missing:
            raise ValueError("signals table is missing required columns: " + ", ".join(sorted(missing)))
        # Identifiers come exclusively from our constant allowlist, not user input.
        projection = ", ".join(
            f'"{field}"' if field in columns else f'NULL AS "{field}"'
            for field in TRADE_FIELDS
        )
        if selected_date is not None:
            query, params = "date = ?", (selected_date,)
        elif start is not None:
            query, params = "date >= ? AND date <= ?", (start, end)
        else:
            selected_date = normalize_trade_date()
            query, params = "date = ?", (selected_date,)
        rows = connection.execute(f"SELECT {projection} FROM signals WHERE status = 'closed' AND {query} ORDER BY date, id", params).fetchall()
        return [dict(row) for row in rows]


def _finite_number(value) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def _rounded(value, digits=2):
    value = _finite_number(value)
    return None if value is None else round(value, digits)


@dataclass(frozen=True)
class OptionContext:
    option_type: str | None = None
    strike: float | None = None
    expiry: str | None = None
    dte: int | None = None
    holding_minutes: int | None = None
    moneyness: str | None = None
    distance_from_strike: float | None = None
    underlying_entry_basis: str = "signal_entry_proxy"


def parse_option_symbol(symbol: str | None) -> dict:
    """Parse known NIFTY YY[M]DD strike type symbols without guessing.

    NSE weekly symbols use e.g. NIFTY2690824050PE (YY=26, M=9, DD=08).
    We validate the date and require exactly one valid split; malformed or
    ambiguous symbols return all-None metadata.
    """
    if not isinstance(symbol, str):
        return {}
    match = re.fullmatch(r"NIFTY(\d{5,6})(\d{5})(CE|PE)", symbol.strip().upper())
    if not match:
        return {}
    expiry_token, strike_token, option_type = match.groups()
    candidates = []
    for month_length in (1, 2):
        if len(expiry_token) != 2 + month_length + 2:
            continue
        year = 2000 + int(expiry_token[:2])
        month = int(expiry_token[2:2 + month_length])
        day = int(expiry_token[2 + month_length:])
        try:
            expiry = date(year, month, day)
        except ValueError:
            continue
        candidates.append(expiry)
    if len(candidates) != 1:
        return {}
    return {"option_type": option_type, "strike": float(int(strike_token)),
            "expiry": candidates[0].isoformat()}


def _holding_minutes(time_signal, exit_time) -> int | None:
    if not isinstance(time_signal, str) or not isinstance(exit_time, str):
        return None
    try:
        start = datetime.strptime(time_signal, "%H:%M:%S")
        end = datetime.strptime(exit_time, "%H:%M:%S")
    except ValueError:
        return None
    minutes = int((end - start).total_seconds() // 60)
    return minutes if minutes >= 0 else None


def market_close_minutes(timestamp: str | None) -> int | None:
    if not isinstance(timestamp, str):
        return None
    try:
        current = datetime.strptime(timestamp, "%H:%M:%S").time()
    except ValueError:
        return None
    return max(0, int((datetime.combine(date.today(), MARKET_CLOSE) -
                       datetime.combine(date.today(), current)).total_seconds() // 60))


def option_context(trade: dict) -> OptionContext:
    parsed = parse_option_symbol(trade.get("options_symbol"))
    underlying = _finite_number(trade.get("entry"))
    strike = parsed.get("strike")
    distance = _rounded(underlying - strike) if underlying is not None and strike is not None else None
    moneyness = None
    if underlying is not None and strike is not None and parsed.get("option_type"):
        # ATM is deliberately a small, explicit proxy band around the strike.
        if abs(underlying - strike) <= 0.5:
            moneyness = "ATM"
        elif parsed["option_type"] == "CE":
            moneyness = "ITM" if underlying > strike else "OTM"
        else:
            moneyness = "ITM" if underlying < strike else "OTM"
    expiry = parsed.get("expiry")
    dte = None
    if expiry and isinstance(trade.get("date"), str):
        try:
            dte = (date.fromisoformat(expiry) - date.fromisoformat(trade["date"])).days
        except ValueError:
            pass
    return OptionContext(option_type=parsed.get("option_type"), strike=strike,
                         expiry=expiry, dte=dte,
                         holding_minutes=_holding_minutes(trade.get("time_signal"), trade.get("exit_time")),
                         moneyness=moneyness, distance_from_strike=distance)


def zone_direction_correct(pnl_points) -> bool | None:
    """Phase 2 proxy only: positive/negative stored index P&L, zero -> unknown.

    Replace this isolated rule when richer 1R/2R/MFE evidence becomes available.
    Do not fall back to result labels, option outcomes, or simulated P&L.
    """
    pnl = _finite_number(pnl_points)
    return None if pnl is None or pnl == 0 else pnl > 0


@dataclass(frozen=True)
class OptionOutcome:
    correct: bool | None
    pnl_points: float | None
    pnl_rupees: float | None


def option_outcome(trade: dict) -> OptionOutcome:
    """Gross BUY-option change; no fees, guessed lot size, or index substitutes.

    Nonpositive entry premiums are invalid/missing fill data; zero exit premium
    is valid (worthless option). Unchanged premiums remain unknown for the
    classifier, which has no option-breakeven label.
    """
    entry = _finite_number(trade.get("options_entry_price"))
    exit_price = _finite_number(trade.get("options_exit_price"))
    if entry is None or entry <= 0 or exit_price is None or exit_price < 0:
        return OptionOutcome(None, None, None)
    points = _rounded(exit_price - entry)
    if points is None:
        return OptionOutcome(None, None, None)
    lot = _finite_number(trade.get("options_lot_size"))
    rupees = None
    if lot is not None and lot > 0 and lot.is_integer():
        rupees = _rounded(points * lot)
    return OptionOutcome(None if points == 0 else points > 0, points, rupees)


def _snapshot_value(value):
    # Keep malformed legacy values as diagnostics without emitting invalid JSON.
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    return value


def reflect_trade(trade: dict) -> KnowledgeEntry:
    """Convert one closed signal snapshot to OBSERVED knowledge, never a rule."""
    if trade.get("status") != "closed":
        raise ValueError("Only status='closed' signals may be reflected")
    signal_id = trade.get("id")
    if type(signal_id) is not int or signal_id <= 0:
        raise ValueError("A positive integer source signal ID is required")
    zone_correct = zone_direction_correct(trade.get("pnl_points"))
    option = option_outcome(trade)
    context = option_context(trade)
    signal_time = trade.get("time_signal")
    fill_time = trade.get("option_fill_time") if isinstance(trade.get("option_fill_time"), str) else None
    greek_time = fill_time or signal_time
    greek_basis = "exact_fill_model" if fill_time and _finite_number(trade.get("underlying_at_option_fill")) is not None else "signal_time_proxy_model"
    greek_underlying = trade.get("underlying_at_option_fill") if greek_basis == "exact_fill_model" else trade.get("entry")
    greek_snapshot = calculate_greeks(_finite_number(trade.get("options_entry_price")),
        greek_underlying, context.strike, context.expiry,
        f"{trade.get('date')}T{greek_time}" if greek_time and trade.get("date") else None,
        context.option_type, basis=greek_basis) if greek_time and context.strike and context.expiry else None
    premium_pct = None
    if option.pnl_points is not None and _finite_number(trade.get("options_entry_price")):
        premium_pct = _rounded(option.pnl_points / float(trade["options_entry_price"]) * 100)
    signal_to_fill = _finite_number(trade.get("signal_to_fill_seconds"))
    if signal_to_fill is None and fill_time:
        signal_to_fill = (_holding_minutes(signal_time, fill_time) * 60
                          if _holding_minutes(signal_time, fill_time) is not None else None)
    fill_underlying = _rounded(trade.get("underlying_at_option_fill"))
    vix = _rounded(trade.get("vix_at_entry"))
    vix_basis = "actual_fill_time" if vix is not None and fill_time else ("signal_time_proxy" if _finite_number(trade.get("vix_at_signal")) is not None else None)
    if vix is None:
        vix = _rounded(trade.get("vix_at_signal"))
    close_basis = "option_fill_time" if fill_time else "signal_time_proxy"
    reflection_input = TradeReflectionInput(
        completed=True, zone_correct=zone_correct, option_correct=option.correct,
        option_pnl=option.pnl_points,
        # False means no confirmed diagnostic flag; it does not establish that
        # entry timing or strike selection was correct. Evidence records UNKNOWN.
        late_entry=False, wrong_strike=False,
    )
    classification = classify_trade(reflection_input)
    evidence = {
        "source_trade_id": signal_id,
        "classification": classification.value,
        "zone_direction_correct": zone_correct,
        "zone_pnl_points": _finite_number(trade.get("pnl_points")),
        "zone_outcome_basis": "sign_of_stored_pnl_points_v1",
        "option_correct": option.correct,
        "option_pnl_points": option.pnl_points,
        "option_pnl_rupees": option.pnl_rupees,
        "signal_time": signal_time,
        "option_fill_time": fill_time,
        "signal_to_fill_seconds": _rounded(signal_to_fill),
        "underlying_at_option_fill": fill_underlying,
        "underlying_fill_basis": trade.get("underlying_fill_basis") if fill_underlying is not None else None,
        "option_entry_price": _rounded(trade.get("options_entry_price")),
        "option_exit_price": _rounded(trade.get("options_exit_price")),
        "option_premium_change": option.pnl_points,
        "option_premium_change_pct": premium_pct,
        "minutes_to_market_close_at_entry": market_close_minutes(fill_time or signal_time),
        "market_close_basis": close_basis,
        "vix_at_entry": vix,
        "vix_basis": vix_basis,
        "iv_entry": greek_snapshot.iv if greek_snapshot else None,
        "delta_entry": greek_snapshot.delta if greek_snapshot else None,
        "gamma_entry": greek_snapshot.gamma if greek_snapshot else None,
        "theta_entry": greek_snapshot.theta if greek_snapshot else None,
        "vega_entry": greek_snapshot.vega if greek_snapshot else None,
        "greeks_timestamp_entry": greek_snapshot.timestamp if greek_snapshot else None,
        "greeks_basis_entry": greek_snapshot.basis if greek_snapshot else None,
        "iv_exit": None, "delta_exit": None, "theta_exit": None,
        "greeks_timestamp_exit": None, "greeks_basis_exit": None,
        "theta_damage": "UNKNOWN", "iv_crush": "UNKNOWN", "low_delta": "UNKNOWN",
        "option_type": context.option_type,
        "strike": _rounded(context.strike),
        "expiry": context.expiry,
        "dte_at_trade_date": context.dte,
        "holding_minutes": context.holding_minutes,
        "moneyness_at_signal": context.moneyness,
        "distance_from_strike": context.distance_from_strike,
        "underlying_entry": _rounded(trade.get("entry")),
        "underlying_entry_basis": context.underlying_entry_basis,
        "time_signal": trade.get("time_signal"),
        "exit_time": trade.get("exit_time"),
        "option_outcome_basis": "gross_actual_premium_change_before_costs",
        "rupee_calculation": "(options_exit_price - options_entry_price) * options_lot_size",
        "late_entry": "UNKNOWN",
        "wrong_strike": "UNKNOWN",
        "diagnostic_reason": "Stored fields do not establish late entry or wrong strike",
        "source_trade": {field: _snapshot_value(trade.get(field)) for field in TRADE_FIELDS},
    }
    return KnowledgeEntry(
        id=f"TRADE-REFLECTION-{signal_id}",
        category="completed_trade_reflection",
        title=f"Completed signal {signal_id}: {classification.value}",
        status=KnowledgeStatus.OBSERVED,
        source=f"sqlite:signals:{signal_id}",
        learning=(f"{classification.value}: single-trade observation using stored index P&L "
                  "and actual option premium change before costs. Missing data stays unknown; "
                  "this is not a validated strategy rule."),
        evidence=(json.dumps(evidence, ensure_ascii=True, allow_nan=False),),
        sample_count=1, confidence=0.0, live_use_allowed=False,
        next_validation=("Review data completeness, costs and independent trade samples; "
                         "historical/out-of-sample validation and human approval remain required."),
    )


def _existing_ids(store: KnowledgeStore) -> set[str]:
    """Read an atomic JSONL snapshot without creating files or touching lock state.

    Phase 1 read_entries() creates the store/lock even for reads. Reuse its parser
    only so dry-run remains strictly non-mutating. Concurrent changes after this
    snapshot are still guarded by append()'s locked duplicate-ID check.
    """
    try:
        text = store.path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    return {entry.id for entry in store._revisions(text)}


@dataclass(frozen=True)
class ReflectionRunResult:
    selected: int
    written: int
    skipped: int
    would_write: int


def reflect_completed_trades(*, db_path: str | Path = DEFAULT_DB_PATH,
                             trade_date: str | None = None,
                             from_date=None, to_date=None,
                             knowledge_path: str | Path = DEFAULT_PATH,
                             dry_run: bool = False,
                             refresh_existing: bool = False,
                             output: TextIO | None = None) -> ReflectionRunResult:
    """Explicit batch runner. Existing IDs are skipped even if the source changed.

    Dry-run prints full candidate KnowledgeEntry payloads and writes nothing.
    Lock contention fails safely; rerunning resumes without duplicating prior work.
    """
    if trade_date is not None and (from_date is not None or to_date is not None):
        raise ValueError("--date cannot be combined with a date range")
    start, end = normalize_date_range(from_date, to_date)
    selected_date = normalize_trade_date(trade_date) if trade_date is not None else (f"{start}..{end}" if start else normalize_trade_date())
    trades = read_completed_trades(db_path, trade_date, start, end)
    store = KnowledgeStore(knowledge_path)
    existing = _existing_ids(store)
    entries = [reflect_trade(trade) for trade in trades]
    stream = output if output is not None else sys.stdout
    written = skipped = would_write = 0
    for entry in entries:
        if entry.id in existing and not refresh_existing:
            skipped += 1
            continue
        if dry_run:
            print(json.dumps({"action": "would_append", "entry": entry.to_dict()},
                             ensure_ascii=True, allow_nan=False), file=stream)
            would_write += 1
        elif refresh_existing and entry.id in existing:
            store.refresh_observed(entry)
            written += 1
        else:
            try:
                store.append(entry)
            except ValueError as exc:
                # Frozen Phase 1 reports duplicate IDs as ValueError. Do not hide
                # schema errors, malformed memory, or other validation failures.
                if str(exc) != f"Duplicate knowledge ID: {entry.id}":
                    raise
                skipped += 1
                existing.add(entry.id)
                continue
            written += 1
            print(json.dumps({"action": "appended", "id": entry.id}), file=stream)
        existing.add(entry.id)
    result = ReflectionRunResult(len(trades), written, skipped, would_write)
    print(json.dumps({"action": "summary", "date": selected_date, "dry_run": dry_run,
                      "selected": result.selected, "written": result.written,
                      "skipped": result.skipped, "would_write": result.would_write}), file=stream)
    return result
