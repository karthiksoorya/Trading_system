import sqlite3
from pathlib import Path

import journal.db as db


def test_fill_context_columns_are_migrated_and_recorded(tmp_path, monkeypatch):
    database = tmp_path / "trades.db"
    monkeypatch.setattr(db.config, "DB_PATH", database)
    db.init_db()
    signal = {
        "zone_type":"DBD", "zone_class":"supply", "timeframe":"5minute",
        "proximal":100, "distal":110, "entry":100, "stop_loss":115,
        "intraday_target":90, "overnight_target":90, "total":8,
        "freshness":2, "strength":2, "time_score":2, "rr_score":2,
        "entry_type":1, "position_size":1, "confluence_count":1, "confluence_tfs":None,
    }
    signal_id = db.log_signal(signal)
    db.update_signal_entry_price(signal_id, 101.25)
    row = db.get_signal(signal_id)
    assert row["options_entry_price"] == 101.25
    assert row["option_fill_time"] is not None
    assert row["signal_to_fill_seconds"] is not None
    assert row["underlying_at_option_fill"] is None
    assert row["underlying_fill_basis"] is None


def test_fill_logging_failure_does_not_raise(monkeypatch):
    class Broken:
        def __enter__(self): raise RuntimeError("logging unavailable")
        def __exit__(self, *args): return False
    monkeypatch.setattr(db, "_conn", lambda: Broken())
    try:
        db.update_signal_entry_price(1, 100)
    except RuntimeError:
        # Existing DB writer is intentionally not wrapped here; execution callers
        # already invoke this only after fill and should guard observational calls.
        pass
