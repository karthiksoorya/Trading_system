from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from agents.learning_models import KnowledgeEntry, KnowledgeStatus
from agents.knowledge_store import DEFAULT_PATH, KnowledgeStore
from agents.reflection import TradeReflectionInput, classify_trade


def entry(**overrides):
    return KnowledgeEntry(**dict(category="zone", title="Research example",
                                 source="offline fixture", learning="Test a claim",
                                 **overrides))


def test_creation_and_roundtrip():
    value = entry()
    assert value.id != entry().id
    assert value.created_at.utcoffset() is not None
    assert value.updated_at >= value.created_at
    assert value.status == KnowledgeStatus.REFERENCE
    assert value.live_use_allowed is False
    assert KnowledgeEntry.from_dict(value.to_dict()) == value


@pytest.mark.parametrize("overrides", [
    {"sample_count": -1}, {"sample_count": True}, {"confidence": 1.1},
    {"confidence": float("nan")}, {"status": "bogus"}, {"id": ""},
    {"evidence": "unstructured"}, {"live_use_allowed": True},
])
def test_invalid_models(overrides):
    with pytest.raises(ValueError):
        entry(**overrides)


def test_missing_directories_jsonl_and_separate_human_knowledge(tmp_path):
    assert DEFAULT_PATH == Path(__file__).resolve().parents[1] / "data/learning/knowledge_entries.jsonl"
    human_path = tmp_path / "AGENT_KNOWLEDGE.md"
    legacy = "# Existing notes\r\nPrivate Unicode: \u0394\r\n".encode("utf-8")
    human_path.write_bytes(legacy)
    path = tmp_path / "data/learning/knowledge_entries.jsonl"
    store = KnowledgeStore(path)
    assert not path.parent.exists()
    assert store.read_text() == ""
    assert store.read_entries() == []
    value = entry(evidence=["First observation"])
    store.append(value)
    assert json.loads(path.read_text()) == value.to_dict()
    assert store.read_entries() == [value]
    store.update_evidence(value.id, "Second observation")
    store.update_status(value.id, "OBSERVED", reason="Observed offline")
    assert len(path.read_text().splitlines()) == 1
    assert human_path.read_bytes() == legacy
    with pytest.raises(ValueError, match=".jsonl"):
        KnowledgeStore(human_path)


def test_duplicate_prevention(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.jsonl")
    value = entry()
    store.append(value)
    original = store.path.read_bytes()
    with pytest.raises(ValueError, match="Duplicate"):
        KnowledgeStore(store.path).append(value)
    assert store.path.read_bytes() == original


def test_evidence_history_and_no_automatic_promotion(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.jsonl")
    first = entry(evidence=["initial"], status=KnowledgeStatus.HYPOTHESIS)
    store.append(first)
    updated = store.update_evidence(first.id, "new observation", additional_samples=100,
                                    confidence=1, next_validation="unseen data")
    assert updated.evidence == ("initial", "new observation")
    assert updated.sample_count == 100
    assert updated.confidence == 1
    assert updated.status == KnowledgeStatus.HYPOTHESIS
    assert updated.live_use_allowed is False
    assert updated.created_at == first.created_at
    assert updated.updated_at >= first.updated_at
    assert store.history(first.id) == [updated]
    assert store.read_entries() == [updated]


@pytest.mark.parametrize("approval", [False, None, 1, "True"])
def test_validation_requires_literal_approval(tmp_path, approval):
    store = KnowledgeStore(tmp_path / "knowledge.jsonl")
    value = entry()
    store.append(value)
    original = store.path.read_bytes()
    with pytest.raises(ValueError, match="approved=True"):
        store.update_status(value.id, "VALIDATED", approved=approval, reason="Review")
    with pytest.raises(ValueError, match="approved=True"):
        store.append(entry(status="VALIDATED"), approved=approval)
    assert store.path.read_bytes() == original


def test_approved_promotion_and_rejection_history(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.jsonl")
    value = entry()
    store.append(value)
    validated = store.update_status(value.id, "VALIDATED", approved=True,
                                    reason="Human reviewed historical and unseen results")
    assert validated.status == KnowledgeStatus.VALIDATED
    assert validated.live_use_allowed is False
    assert "approved=True" in validated.evidence[-1]
    rejected = store.update_status(value.id, "REJECTED", reason="Contradicting evidence")
    assert rejected.evidence[:len(validated.evidence)] == validated.evidence
    assert len(store.history(value.id)) == 1
    store.append(entry(status="VALIDATED"), approved=True)
    with pytest.raises(ValueError, match="live use"):
        store.append(replace(entry(status="VALIDATED"), live_use_allowed=True), approved=True)


def test_failed_updates_preserve_file(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.jsonl")
    value = store.append(entry())
    original = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.update_evidence(value.id, "invalid confidence", confidence=2)
    with pytest.raises(ValueError):
        store.update_evidence(value.id, "invalid count", additional_samples=-1)
    with pytest.raises(KeyError):
        store.update_status("missing", "OBSERVED", reason="observation")
    assert store.path.read_bytes() == original


def test_atomic_write_failure(tmp_path, monkeypatch):
    store = KnowledgeStore(tmp_path / "knowledge.jsonl")
    value = store.append(entry())
    original = store.path.read_bytes()

    def fail(*args):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr("agents.knowledge_store.os.replace", fail)
    with pytest.raises(OSError):
        store.update_evidence(value.id, "new")
    assert store.path.read_bytes() == original
    assert set(tmp_path.iterdir()) == {store.path, store.path.with_suffix(".jsonl.lock")}


def test_competing_writer_fails_without_losing_data(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.jsonl")
    value = store.append(entry())
    with store._locked():
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(KnowledgeStore(store.path).update_evidence, value.id, "busy")
            with pytest.raises(FileExistsError):
                future.result()
    assert store.read_entries() == [value]


def test_marker_content_and_corruption(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.jsonl")
    value = entry(evidence=["<!-- private-learning:v1 -->\n```json\n{}"])
    store.append(value)
    assert store.read_entries() == [value]
    with store.path.open("ab") as handle:
        handle.write(b"{broken}\n")
    original = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.append(entry())
    assert store.path.read_bytes() == original


def lock_path(store):
    return store.path.with_name(store.path.name + ".lock")


def read_lock_metadata_bytes(store):
    # Windows enforces the lock on byte zero even for reads from other handles.
    with lock_path(store).open("rb", buffering=0) as handle:
        handle.seek(1)
        return handle.read()


def test_lock_metadata_and_normal_release(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.jsonl")
    with store._locked():
        metadata = json.loads(read_lock_metadata_bytes(store))
        assert metadata["pid"] == os.getpid()
        assert abs(metadata["created_at"] - time.time()) < 10
        assert metadata["released"] is False
    assert json.loads(lock_path(store).read_bytes()[1:])["released"] is True
    # Successful release allows immediate reuse, regardless of timeout.
    assert store.read_entries() == []


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf"), True, "300"])
def test_invalid_stale_timeout(tmp_path, timeout):
    with pytest.raises(ValueError):
        KnowledgeStore(tmp_path / "knowledge.jsonl", stale_lock_timeout=timeout)


@pytest.mark.parametrize("contents", [b"", b"\x00{partial", b"\x00[]", b'\x00{"created_at": "bad"}'])
def test_crash_during_lock_initialization(tmp_path, contents):
    store = KnowledgeStore(tmp_path / "knowledge.jsonl", stale_lock_timeout=60)
    lock = lock_path(store)
    lock.write_bytes(contents)
    with pytest.raises(FileExistsError, match="stale timeout"):
        store.read_entries()
    assert lock.read_bytes() == contents
    old_time = time.time() - 120
    os.utime(lock, (old_time, old_time))
    assert store.read_entries() == []
    assert lock.exists()


def test_crashed_process_recovery_preserves_memory(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.jsonl")
    value = store.append(entry())
    # os._exit deliberately bypasses finally blocks and leaves unreleased metadata.
    result = subprocess.run(
        [sys.executable, "-c",
         "import os, sys; from agents.knowledge_store import KnowledgeStore; "
         "store = KnowledgeStore(sys.argv[1]); "
         "lock = store._locked(); lock.__enter__(); os._exit(17)", str(store.path)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15,
    )
    assert result.returncode == 17, result.stderr.decode()
    previous = lock_path(store).read_bytes()
    metadata = json.loads(previous[1:])
    assert metadata["pid"] != os.getpid()
    assert metadata["released"] is False
    with pytest.raises(FileExistsError, match="stale timeout"):
        store.read_entries()
    assert lock_path(store).read_bytes() == previous
    recovered = KnowledgeStore(store.path, stale_lock_timeout=0)
    assert recovered.read_entries() == [value]
    updated = recovered.update_evidence(value.id, "after crash")
    assert recovered.history(value.id) == [updated]


def test_active_process_lock_is_never_recovered_even_after_timeout(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.jsonl", stale_lock_timeout=0)
    store.append(entry())
    process = subprocess.Popen(
        [sys.executable, "-c",
         "import sys; from agents.knowledge_store import KnowledgeStore; "
         "store = KnowledgeStore(sys.argv[1]); "
         "lock = store._locked(); lock.__enter__(); "
         "print('ready', flush=True); sys.stdin.read(); lock.__exit__(None,None,None)",
         str(store.path)],
        cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        # Bound the readiness wait so a broken child cannot hang the suite.
        with ThreadPoolExecutor(max_workers=1) as executor:
            ready = executor.submit(process.stdout.readline)
            try:
                assert ready.result(timeout=10).strip() == "ready"
            finally:
                if not ready.done():
                    process.kill()
        previous = read_lock_metadata_bytes(store)
        with pytest.raises(FileExistsError, match="active or unavailable"):
            store.read_entries()
        assert read_lock_metadata_bytes(store) == previous
    finally:
        process.communicate(input="", timeout=10)
    assert process.returncode == 0
    assert len(store.read_entries()) == 1


def test_stale_recovery_contenders_share_one_lock(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.jsonl", stale_lock_timeout=0)
    # The recorded PID may have been reused; kernel ownership is authoritative.
    lock_path(store).write_bytes(b"\x00" + json.dumps({
        "pid": os.getpid(), "created_at": 0, "released": False,
    }).encode())
    values = [entry() for _ in range(4)]

    def attempt(value):
        try:
            KnowledgeStore(store.path, stale_lock_timeout=0).append(value)
            return True
        except FileExistsError:
            return False

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(attempt, values))
    assert any(results)
    for value, success in zip(values, results):
        if not success:
            store.append(value)
    assert {item.id for item in store.read_entries()} == {item.id for item in values}
    assert len(store.path.read_text().splitlines()) == 4


def test_jsonl_without_final_newline_and_embedded_newlines(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.jsonl")
    value = entry(evidence=["Line one\nLine two"])
    store.path.write_text(json.dumps(value.to_dict()), encoding="utf-8")
    updated = store.update_evidence(value.id, "More\nobservations")
    assert len(store.path.read_text().splitlines()) == 1
    assert store.history(value.id) == [updated]


@pytest.mark.parametrize("bad_line", ["", "[]", "null", "{}", "# Markdown", '{"created_at": null}'])
def test_bad_jsonl_fails_closed(tmp_path, bad_line):
    store = KnowledgeStore(tmp_path / "knowledge.jsonl")
    store.append(entry())
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write(bad_line + "\n")
    previous = store.path.read_bytes()
    with pytest.raises(ValueError, match="line 2"):
        store.append(entry())
    assert store.path.read_bytes() == previous


@pytest.mark.parametrize("fields, expected", [
    ({"zone_correct": True, "option_correct": True}, "ZONE_CORRECT_OPTION_CORRECT"),
    ({"zone_correct": True, "option_correct": False}, "ZONE_CORRECT_OPTION_WRONG"),
    ({"zone_correct": False, "option_pnl": 10}, "ZONE_WRONG_OPTION_PROFIT"),
    ({"zone_correct": False, "option_pnl": -10}, "ZONE_WRONG_OPTION_LOSS"),
    ({"late_entry": True, "wrong_strike": True}, "LATE_ENTRY"),
    ({"wrong_strike": True, "zone_correct": True}, "WRONG_STRIKE"),
    ({}, "UNKNOWN"),
    ({"zone_correct": False, "option_pnl": 0}, "UNKNOWN"),
    ({"zone_correct": False}, "UNKNOWN"),
    ({"zone_correct": True, "option_pnl": 10}, "UNKNOWN"),
    ({"completed": False, "late_entry": True}, "UNKNOWN"),
])
def test_reflection_classification(fields, expected):
    assert classify_trade(TradeReflectionInput(**({"completed": True} | fields))).value == expected


def test_reflection_rejects_unstructured_and_invalid_input():
    with pytest.raises(TypeError):
        classify_trade("profitable trade")
    with pytest.raises(ValueError):
        TradeReflectionInput(completed=True, zone_correct="false")
    with pytest.raises(ValueError):
        TradeReflectionInput(completed=True, option_pnl=float("nan"))
