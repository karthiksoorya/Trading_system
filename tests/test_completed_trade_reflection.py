"""Phase 2 integration tests use temporary journals only; no live DB access."""

from contextlib import closing
from datetime import date
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from agents.completed_trade_reflection import (
    DEFAULT_DB_PATH, TRADE_FIELDS, read_completed_trades, reflect_completed_trades,
    reflect_trade, zone_direction_correct, parse_option_symbol, option_context,
)
from agents.knowledge_store import KnowledgeStore
from agents.learning_models import KnowledgeStatus


TRADE_DATE = "2026-09-04"
SCHEMA = """
CREATE TABLE signals (
    id INTEGER PRIMARY KEY, status TEXT, date TEXT, time_signal TEXT,
    zone_type TEXT, zone_class TEXT, timeframe TEXT,
    proximal REAL, distal REAL, entry REAL, stop_loss REAL, intraday_target REAL,
    booster_score REAL, freshness REAL, strength REAL, time_score REAL, rr_score REAL,
    confluence_count INTEGER, exit_time TEXT, exit_price REAL, exit_reason TEXT,
    pnl_points REAL, result TEXT, options_symbol TEXT, options_entry_price REAL,
    options_exit_price REAL, options_lot_size INTEGER, mode TEXT, notes TEXT,
    closed_by TEXT, sim_pnl_points REAL, sim_outcome TEXT
)
"""


def sample_trade(**overrides):
    row = {
        "id": 42, "status": "closed", "date": TRADE_DATE, "time_signal": "10:00:00",
        "zone_type": "DBD", "zone_class": "supply", "timeframe": "5minute",
        "proximal": 25000.0, "distal": 25020.0, "entry": 25000.0,
        "stop_loss": 25025.0, "intraday_target": 24960.0, "booster_score": 10.0,
        "freshness": 3.0, "strength": 2.0, "time_score": 2.0, "rr_score": 3.0,
        "confluence_count": 3, "exit_time": "10:15:00", "exit_price": 24960.0,
        "exit_reason": "target", "pnl_points": 40.0, "result": "win",
        "options_symbol": "SYNTHETIC-NIFTY-PE", "options_entry_price": 100.0,
        "options_exit_price": 110.0, "options_lot_size": 65, "mode": "paper",
        "notes": "Synthetic test fixture", "closed_by": "system",
    }
    return row | overrides


def make_db(path, rows, schema=SCHEMA):
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(schema)
        for row in rows:
            columns = ", ".join(f'"{key}"' for key in row)
            placeholders = ", ".join("?" for _ in row)
            connection.execute(f"INSERT INTO signals ({columns}) VALUES ({placeholders})",
                               tuple(row.values()))
        connection.commit()
    return path

def test_inclusive_date_ranges_and_validation(tmp_path):
    path=make_db(tmp_path/"range.db", [sample_trade(id=1,date="2026-09-01"), sample_trade(id=2,date="2026-09-02"), sample_trade(id=3,date="2026-09-03"), sample_trade(id=4,date="2026-09-04")])
    assert [r["id"] for r in read_completed_trades(path, from_date="2026-09-01", to_date="2026-09-04")] == [1,2,3,4]
    assert [r["id"] for r in read_completed_trades(path, from_date="2026-09-02", to_date="2026-09-02")] == [2]
    with pytest.raises(ValueError): read_completed_trades(path, from_date="2026-09-04", to_date="2026-09-01")
    with pytest.raises(ValueError): read_completed_trades(path, TRADE_DATE, from_date="2026-09-01", to_date="2026-09-04")


def evidence(entry):
    return json.loads(entry.evidence[0])


@pytest.mark.parametrize("symbol, expected", [
    ("NIFTY2690824050CE", {"option_type": "CE", "strike": 24050.0, "expiry": "2026-09-08"}),
    ("NIFTY2690824050PE", {"option_type": "PE", "strike": 24050.0, "expiry": "2026-09-08"}),
])
def test_option_symbol_parsing(symbol, expected):
    assert parse_option_symbol(symbol) == expected


def test_option_context_dte_holding_and_moneyness():
    row = sample_trade(options_symbol="NIFTY2690824050PE", date="2026-09-04",
                       time_signal="11:11:27", exit_time="12:05:39", entry=24002.45)
    context = option_context(row)
    assert context.dte == 4
    assert context.holding_minutes == 54
    assert context.moneyness == "ITM"
    assert context.distance_from_strike == -47.55
    assert context.underlying_entry_basis == "signal_entry_proxy"
    assert option_context(sample_trade(options_symbol="NIFTY2690824050CE", entry=24050.0)).moneyness == "ATM"
    assert option_context(sample_trade(options_symbol="NIFTY2690824050CE", entry=23900.0)).moneyness == "OTM"


@pytest.mark.parametrize("symbol", [None, "", "NIFTYBADPE", "NIFTY2693224050PE", "NIFTY2690824050"])
def test_malformed_option_symbol_is_unknown(symbol):
    context = option_context(sample_trade(options_symbol=symbol))
    assert parse_option_symbol(symbol) == {}
    assert context.option_type is None
    assert context.strike is None
    assert context.expiry is None
    assert context.moneyness is None
    assert context.distance_from_strike is None


def test_missing_timestamps_leave_holding_unknown():
    context = option_context(sample_trade(time_signal=None, exit_time=None))
    assert context.holding_minutes is None
    evidence_value = evidence(reflect_trade(sample_trade(time_signal=None, exit_time=None)))
    assert evidence_value["time_signal"] is None
    assert evidence_value["exit_time"] is None
    assert evidence_value["holding_minutes"] is None


@pytest.mark.parametrize("zone_pnl, option_exit, expected", [
    (40, 110, "ZONE_CORRECT_OPTION_CORRECT"),
    (40, 90, "ZONE_CORRECT_OPTION_WRONG"),
    (-40, 110, "ZONE_WRONG_OPTION_PROFIT"),
    (-40, 90, "ZONE_WRONG_OPTION_LOSS"),
])
def test_independent_zone_and_option_outcomes(zone_pnl, option_exit, expected):
    entry = reflect_trade(sample_trade(pnl_points=zone_pnl, options_exit_price=option_exit))
    result = evidence(entry)
    assert result["classification"] == expected
    assert result["zone_pnl_points"] == zone_pnl
    assert result["option_pnl_points"] == option_exit - 100
    assert result["zone_direction_correct"] == (zone_pnl > 0)
    assert result["option_correct"] == (option_exit > 100)
    assert entry.status == KnowledgeStatus.OBSERVED
    assert entry.live_use_allowed is False
    assert entry.sample_count == 1
    assert entry.id == "TRADE-REFLECTION-42"
    assert entry.confidence == 0


@pytest.mark.parametrize("premiums", [
    {"options_entry_price": None}, {"options_exit_price": None},
    {"options_entry_price": None, "options_exit_price": None},
])
def test_missing_option_prices_preserve_zone(premiums):
    result = evidence(reflect_trade(sample_trade(**premiums)))
    assert result["classification"] == "UNKNOWN"
    assert result["zone_direction_correct"] is True
    assert result["zone_pnl_points"] == 40
    assert result["option_correct"] is None
    assert result["option_pnl_points"] is None
    assert result["option_pnl_rupees"] is None


@pytest.mark.parametrize("pnl", [0, None, float("nan"), float("inf"), "invalid"])
def test_zero_or_insufficient_zone_pnl_is_unknown(pnl):
    result = evidence(reflect_trade(sample_trade(pnl_points=pnl)))
    assert result["classification"] == "UNKNOWN"
    assert result["zone_direction_correct"] is None
    assert result["option_correct"] is True
    assert result["option_pnl_points"] == 10
    assert zone_direction_correct(pnl) is None


@pytest.mark.parametrize("lot, expected", [
    (65, 650), (130, 1300), (None, None), (0, None), (-1, None),
    (2.5, None), (float("inf"), None), ("unknown", None),
])
def test_rupee_estimate_uses_only_available_valid_lot(lot, expected):
    result = evidence(reflect_trade(sample_trade(options_lot_size=lot)))
    assert result["option_pnl_rupees"] == expected
    assert result["option_pnl_points"] == 10
    assert result["option_outcome_basis"] == "gross_actual_premium_change_before_costs"


def test_zero_exit_is_valid_but_zero_change_is_not_a_win():
    worthless = evidence(reflect_trade(sample_trade(options_exit_price=0)))
    assert worthless["option_pnl_points"] == -100
    assert worthless["option_pnl_rupees"] == -6500
    assert worthless["classification"] == "ZONE_CORRECT_OPTION_WRONG"
    unchanged = evidence(reflect_trade(sample_trade(options_exit_price=100)))
    assert unchanged["option_pnl_points"] == 0
    assert unchanged["option_pnl_rupees"] == 0
    assert unchanged["classification"] == "UNKNOWN"


@pytest.mark.parametrize("overrides", [
    {"options_entry_price": 0}, {"options_entry_price": -5},
    {"options_exit_price": -1}, {"options_exit_price": float("inf")},
    {"options_entry_price": "invalid"},
])
def test_invalid_premiums_do_not_invent_option_outcomes(overrides):
    result = evidence(reflect_trade(sample_trade(**overrides)))
    assert result["classification"] == "UNKNOWN"
    assert result["option_pnl_points"] is None
    assert result["option_pnl_rupees"] is None


def test_metadata_timestamps_and_unconfirmed_diagnostics_are_retained():
    row = sample_trade(notes="late entry? wrong strike?", exit_reason="manual")
    entry = reflect_trade(row)
    result = evidence(entry)
    assert result["source_trade_id"] == row["id"]
    assert {k: result["source_trade"][k] for k in row} == row
    assert entry.source == "sqlite:signals:42"
    assert entry.created_at.utcoffset() is not None
    assert entry.updated_at >= entry.created_at
    assert result["late_entry"] == result["wrong_strike"] == "UNKNOWN"
    assert result["classification"] == "ZONE_CORRECT_OPTION_CORRECT"


def test_reader_filters_closed_date_and_exposes_requested_fields_read_only(tmp_path, monkeypatch):
    path = make_db(tmp_path / "journal #1.db", [
        sample_trade(), sample_trade(id=43, status="approved"),
        sample_trade(id=44, status="rejected"), sample_trade(id=45, status="expired"),
        sample_trade(id=46, status="closed", date="2026-09-03"),
    ])
    before = path.read_bytes()
    original_connect = sqlite3.connect
    writes_denied = []

    def readonly_connect(*args, **kwargs):
        assert args[0].endswith("?mode=ro") and kwargs["uri"] is True
        connection = original_connect(*args, **kwargs)
        # Prove the connection itself rejects writes, not merely that SELECT was used.
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("UPDATE signals SET status='pending'")
        writes_denied.append(True)
        return connection

    monkeypatch.setattr("agents.completed_trade_reflection.sqlite3.connect", readonly_connect)
    rows = read_completed_trades(path, TRADE_DATE)
    assert writes_denied == [True]
    assert [row["id"] for row in rows] == [42]
    assert set(rows[0]) == set(TRADE_FIELDS)
    assert {k: rows[0][k] for k in sample_trade()} == sample_trade()
    assert path.read_bytes() == before


def test_default_date_matches_journal_local_today(tmp_path):
    path = make_db(tmp_path / "journal.db", [sample_trade(date=date.today().isoformat())])
    assert DEFAULT_DB_PATH == Path(__file__).resolve().parents[1] / "data/trades.db"
    assert len(read_completed_trades(path)) == 1


def test_repeated_run_is_idempotent_even_if_source_is_later_enriched(tmp_path):
    path = make_db(tmp_path / "journal.db", [sample_trade(options_exit_price=None)])
    knowledge = tmp_path / "learning/knowledge_entries.jsonl"
    first = reflect_completed_trades(db_path=path, trade_date=TRADE_DATE,
                                     knowledge_path=knowledge, output=io.StringIO())
    original = knowledge.read_bytes()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE signals SET options_exit_price=110 WHERE id=42")
        connection.commit()
    second = reflect_completed_trades(db_path=path, trade_date=TRADE_DATE,
                                      knowledge_path=knowledge, output=io.StringIO())
    assert first.written == 1 and second.written == 0 and second.skipped == 1
    assert knowledge.read_bytes() == original
    entries = KnowledgeStore(knowledge).read_entries()
    assert len(entries) == 1
    assert evidence(entries[0])["classification"] == "UNKNOWN"


def test_refresh_existing_updates_enrichment_without_duplicate_id(tmp_path):
    path = make_db(tmp_path / "journal.db", [sample_trade(options_exit_price=None)])
    knowledge = tmp_path / "learning/knowledge_entries.jsonl"
    reflect_completed_trades(db_path=path, trade_date=TRADE_DATE,
                             knowledge_path=knowledge, output=io.StringIO())
    original = KnowledgeStore(knowledge).read_entries()[0]
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE signals SET options_exit_price=110 WHERE id=42")
        connection.commit()
    refreshed = reflect_completed_trades(db_path=path, trade_date=TRADE_DATE,
                                         knowledge_path=knowledge, refresh_existing=True,
                                         output=io.StringIO())
    assert refreshed.written == 1
    store = KnowledgeStore(knowledge)
    current = store.read_entries()[0]
    assert current.id == original.id
    assert current.created_at == original.created_at
    assert current.updated_at >= original.updated_at
    assert current.status == KnowledgeStatus.OBSERVED
    assert current.live_use_allowed is False
    details = evidence(current)
    assert details["option_pnl_points"] == 10
    assert details["option_pnl_rupees"] == 650
    assert len(store.history(current.id)) == 1
    normal = reflect_completed_trades(db_path=path, trade_date=TRADE_DATE,
                                      knowledge_path=knowledge, output=io.StringIO())
    assert normal.written == 0 and normal.skipped == 1


@pytest.mark.parametrize("existing_store", [False, True])
def test_dry_run_never_creates_or_changes_memory_or_lock(tmp_path, existing_store):
    path = make_db(tmp_path / "journal.db", [sample_trade()])
    knowledge = tmp_path / "learning/knowledge_entries.jsonl"
    if existing_store:
        KnowledgeStore(knowledge).append(reflect_trade(sample_trade(id=41)))
    before = {file.relative_to(tmp_path): file.read_bytes()
              for file in tmp_path.rglob("*") if file.is_file()}
    directories = {file.relative_to(tmp_path) for file in tmp_path.rglob("*") if file.is_dir()}
    output = io.StringIO()
    result = reflect_completed_trades(db_path=path, trade_date=TRADE_DATE,
                                      knowledge_path=knowledge, dry_run=True, output=output)
    assert result.written == 0 and result.would_write == 1
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert messages[0]["action"] == "would_append"
    assert messages[0]["entry"]["id"] == "TRADE-REFLECTION-42"
    assert messages[0]["entry"]["status"] == "OBSERVED"
    assert messages[0]["entry"]["live_use_allowed"] is False
    assert messages[1]["action"] == "summary"
    assert before == {file.relative_to(tmp_path): file.read_bytes()
                      for file in tmp_path.rglob("*") if file.is_file()}
    assert directories == {file.relative_to(tmp_path) for file in tmp_path.rglob("*") if file.is_dir()}


def test_dry_run_skips_existing_trade_without_touching_lock(tmp_path):
    path = make_db(tmp_path / "journal.db", [sample_trade()])
    knowledge = tmp_path / "knowledge.jsonl"
    store = KnowledgeStore(knowledge)
    store.append(reflect_trade(sample_trade()))
    original = knowledge.read_bytes()
    # Hold a valid active lock: dry-run must not acquire, recover or release it.
    with store._locked():
        result = reflect_completed_trades(db_path=path, trade_date=TRADE_DATE,
                                          knowledge_path=knowledge, dry_run=True,
                                          output=io.StringIO())
    assert result.skipped == 1 and result.would_write == 0
    assert knowledge.read_bytes() == original


def test_legacy_schema_missing_optional_columns_preserves_paper_observation(tmp_path):
    path = make_db(tmp_path / "legacy.db", [{
        "id": 1, "status": "closed", "date": TRADE_DATE, "pnl_points": -12, "mode": "paper",
    }], schema="CREATE TABLE signals (id INTEGER PRIMARY KEY, status TEXT, date TEXT, pnl_points REAL, mode TEXT)")
    original = path.read_bytes()
    row = read_completed_trades(path, TRADE_DATE)[0]
    assert set(row) == set(TRADE_FIELDS)
    assert row["options_entry_price"] is None
    assert row["time_signal"] is None
    result = evidence(reflect_trade(row))
    assert result["zone_direction_correct"] is False
    assert result["option_correct"] is None
    assert result["classification"] == "UNKNOWN"
    assert result["source_trade"]["mode"] == "paper"
    assert result["source_trade"]["options_symbol"] is None
    assert path.read_bytes() == original


def test_result_and_simulated_data_are_not_option_or_zone_substitutes(tmp_path):
    path = make_db(tmp_path / "journal.db", [sample_trade(
        pnl_points=None, result="win", options_entry_price=None, options_exit_price=None,
        sim_pnl_points=100, sim_outcome="target",
    )])
    result = evidence(reflect_trade(read_completed_trades(path, TRADE_DATE)[0]))
    assert result["classification"] == "UNKNOWN"
    assert result["zone_direction_correct"] is result["option_correct"] is None


def test_missing_database_bad_schema_and_invalid_date_fail_without_creating_store(tmp_path):
    knowledge = tmp_path / "learning/knowledge.jsonl"
    with pytest.raises(FileNotFoundError):
        reflect_completed_trades(db_path=tmp_path / "missing.db", knowledge_path=knowledge)
    assert not (tmp_path / "missing.db").exists()
    path = make_db(tmp_path / "legacy.db", [], schema="CREATE TABLE signals (id INTEGER)")
    with pytest.raises(ValueError, match="required columns"):
        reflect_completed_trades(db_path=path, knowledge_path=knowledge)
    with pytest.raises(ValueError):
        read_completed_trades(path, "2026-09-04' OR 1=1 --")
    assert not knowledge.parent.exists()


def test_corrupt_memory_is_not_silently_skipped(tmp_path):
    path = make_db(tmp_path / "journal.db", [sample_trade()])
    knowledge = tmp_path / "knowledge.jsonl"
    knowledge.write_bytes(b"{broken}\n")
    for dry_run in (True, False):
        with pytest.raises(ValueError, match="Malformed knowledge"):
            reflect_completed_trades(db_path=path, trade_date=TRADE_DATE,
                                     knowledge_path=knowledge, dry_run=dry_run)
    assert knowledge.read_bytes() == b"{broken}\n"


def test_duplicate_insert_race_is_skipped_but_other_errors_propagate(tmp_path, monkeypatch):
    path = make_db(tmp_path / "journal.db", [sample_trade()])
    knowledge = tmp_path / "knowledge.jsonl"
    original_append = KnowledgeStore.append

    def competing_append(store, entry):
        original_append(store, entry)  # Another runner wins after the initial snapshot.
        return original_append(store, entry)

    monkeypatch.setattr(KnowledgeStore, "append", competing_append)
    result = reflect_completed_trades(db_path=path, trade_date=TRADE_DATE,
                                      knowledge_path=knowledge, output=io.StringIO())
    assert result.skipped == 1
    assert len(KnowledgeStore(knowledge).history("TRADE-REFLECTION-42")) == 1

    def fail(store, entry):
        raise ValueError("Unrelated validation failure")

    monkeypatch.setattr(KnowledgeStore, "append", fail)
    with pytest.raises(ValueError, match="Unrelated"):
        reflect_completed_trades(db_path=path, trade_date=TRADE_DATE,
                                 knowledge_path=tmp_path / "other.jsonl")


@pytest.mark.parametrize("launch", [
    ["-m", "agents.reflect_completed_trades"], ["agents/reflect_completed_trades.py"],
])
def test_cli_dry_run_and_explicit_runner(tmp_path, launch):
    path = make_db(tmp_path / "journal.db", [sample_trade()])
    knowledge = tmp_path / "learning/knowledge.jsonl"
    command = [sys.executable, *launch, "--db", str(path), "--date", TRADE_DATE,
               "--knowledge-path", str(knowledge)]
    root = Path(__file__).resolve().parents[1]
    dry = subprocess.run(command + ["--dry-run"], cwd=root, capture_output=True, text=True, timeout=15)
    assert dry.returncode == 0, dry.stderr
    messages = [json.loads(line) for line in dry.stdout.splitlines()]
    assert json.loads(messages[0]["entry"]["evidence"][0])["option_pnl_rupees"] == 650
    assert messages[-1]["would_write"] == 1
    assert not knowledge.parent.exists()
    actual = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=15)
    assert actual.returncode == 0, actual.stderr
    assert json.loads(actual.stdout.splitlines()[-1])["written"] == 1
    again = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=15)
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout.splitlines()[-1])["skipped"] == 1
    assert len(knowledge.read_text().splitlines()) == 1


@pytest.mark.parametrize("row", [sample_trade(status="approved"), sample_trade(id=None)])
def test_reflection_rejects_unclosed_or_unidentified_rows(row):
    with pytest.raises(ValueError):
        reflect_trade(row)
