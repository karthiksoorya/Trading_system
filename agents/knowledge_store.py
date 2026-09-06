"""Private machine JSONL memory with complete, append-only revision history.

AGENT_KNOWLEDGE.md is a separate human-maintained document, never read or written
here. No live-system imports or writes. Locks require a local filesystem supporting
OS advisory locks; manual editors and cloud sync do not participate in locking.
"""

from contextlib import contextmanager
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import tempfile
import time

from agents.learning_models import KnowledgeEntry, KnowledgeStatus, utc_now


DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data/learning/knowledge_entries.jsonl"


def _lock_descriptor(descriptor: int):
    """Take a nonblocking kernel lock, automatically released on process death."""
    if os.name == "nt":
        import msvcrt
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_descriptor(descriptor: int):
    if os.name == "nt":
        import msvcrt
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(descriptor, fcntl.LOCK_UN)


class KnowledgeStore:
    def __init__(self, path: str | Path = DEFAULT_PATH, *,
                 stale_lock_timeout: float = 300.0):
        self.path = Path(path)
        if self.path.suffix.lower() != ".jsonl":
            raise ValueError("Machine knowledge requires a .jsonl path, not human Markdown")
        if (type(stale_lock_timeout) not in (int, float)
                or not math.isfinite(stale_lock_timeout) or stale_lock_timeout < 0):
            raise ValueError("stale_lock_timeout must be finite, nonnegative seconds")
        self.stale_lock_timeout = stale_lock_timeout

    @staticmethod
    def _write_lock_metadata(handle, metadata):
        # Byte zero is reserved for the Windows kernel lock, metadata starts at 1.
        handle.seek(1)
        handle.write(json.dumps(metadata).encode("utf-8"))
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())

    def _check_previous_lock(self, handle, newly_created):
        if newly_created:
            return
        handle.seek(1)
        try:
            metadata = json.loads(handle.read())
            if not isinstance(metadata, dict):
                raise ValueError("Invalid metadata")
            if metadata.get("released") is True:
                return
            created_at = metadata["created_at"]
            if type(created_at) not in (int, float) or not math.isfinite(created_at):
                raise ValueError("Invalid lock creation time")
        except (ValueError, KeyError, UnicodeDecodeError):
            # Crash during lock initialization: use last file write as the age.
            created_at = os.fstat(handle.fileno()).st_mtime
        if time.time() - created_at < self.stale_lock_timeout:
            raise FileExistsError("Unreleased knowledge lock has not reached stale timeout")
        # We already hold the kernel lock: no active cooperating writer owns it.
        # PID alone cannot prove ownership (PID reuse, permissions, or live process
        # with an abandoned handle). Never unlink: that creates split-lock races.

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_name(self.path.name + ".lock")
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            newly_created = True
        except FileExistsError:
            descriptor = os.open(lock, os.O_RDWR)
            newly_created = False
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        acquired = False
        try:
            try:
                _lock_descriptor(descriptor)
            except OSError as exc:
                raise FileExistsError("Knowledge store lock is active or unavailable") from exc
            acquired = True
            self._check_previous_lock(handle, newly_created)
            metadata = {"pid": os.getpid(), "created_at": time.time(), "released": False}
            self._write_lock_metadata(handle, metadata)
            try:
                yield
            finally:
                self._write_lock_metadata(handle, {**metadata, "released": True})
        finally:
            try:
                if acquired:
                    _unlock_descriptor(descriptor)
            finally:
                handle.close()

    def _write(self, contents: bytes):
        descriptor, name = tempfile.mkstemp(prefix=self.path.name + ".",
                                            suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(contents)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _ensure(self):
        if not self.path.exists():
            self._write(b"")

    def read_text(self) -> str:
        with self._locked():
            self._ensure()
            return self.path.read_bytes().decode("utf-8")

    @staticmethod
    def _revisions(text: str) -> list[KnowledgeEntry]:
        revisions = []
        for number, line in enumerate(text.splitlines(), 1):
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise ValueError("Revision must be an object")
                revisions.append(KnowledgeEntry.from_dict(payload))
            except (ValueError, TypeError, KeyError) as exc:
                raise ValueError(f"Malformed knowledge revision on line {number}; refusing to modify file") from exc
        return revisions

    def history(self, entry_id: str) -> list[KnowledgeEntry]:
        return [entry for entry in self._revisions(self.read_text()) if entry.id == entry_id]

    def read_entries(self) -> list[KnowledgeEntry]:
        return list({entry.id: entry for entry in
                     self._revisions(self.read_text())}.values())

    def _append_revision(self, original: bytes, entry: KnowledgeEntry):
        payload = json.dumps(entry.to_dict(), ensure_ascii=True, allow_nan=False)
        separator = "\n" if original and not original.endswith(b"\n") else ""
        block = separator + payload + "\n"
        self._write(original + block.encode("utf-8"))

    @staticmethod
    def _jsonl(entries: list[KnowledgeEntry]) -> bytes:
        return ("".join(json.dumps(entry.to_dict(), ensure_ascii=True,
                                    allow_nan=False) + "\n" for entry in entries)).encode("utf-8")

    def _replace_locked(self, entry: KnowledgeEntry) -> KnowledgeEntry:
        """Replace all physical copies of an ID in one atomic JSONL rewrite."""
        original = self.path.read_bytes()
        revisions = self._revisions(original.decode("utf-8"))
        matching = [current for current in revisions if current.id == entry.id]
        if not matching:
            raise KeyError(entry.id)
        earliest = min(current.created_at for current in matching)
        replacement = replace(entry, created_at=earliest, updated_at=max(utc_now(),
                            max(current.updated_at for current in matching)),
                              live_use_allowed=False)
        output = []
        replaced = False
        for current in revisions:
            if current.id == entry.id:
                if not replaced:
                    output.append(replacement)
                    replaced = True
            else:
                output.append(current)
        self._write(self._jsonl(output))
        return replacement

    def append(self, entry: KnowledgeEntry, *, approved: bool = False) -> KnowledgeEntry:
        if not isinstance(entry, KnowledgeEntry):
            raise TypeError("entry must be KnowledgeEntry")
        if entry.status == KnowledgeStatus.VALIDATED and approved is not True:
            raise ValueError("VALIDATED requires explicit approved=True")
        if entry.live_use_allowed:
            raise ValueError("Phase one cannot enable live use")
        with self._locked():
            self._ensure()
            original = self.path.read_bytes()
            entries = self._revisions(original.decode("utf-8"))
            if any(previous.id == entry.id for previous in entries):
                raise ValueError(f"Duplicate knowledge ID: {entry.id}")
            self._append_revision(original, entry)
        return entry

    def refresh_observed(self, entry: KnowledgeEntry) -> KnowledgeEntry:
        """Atomically append a recomputed OBSERVED snapshot for an existing ID."""
        if not isinstance(entry, KnowledgeEntry):
            raise TypeError("entry must be KnowledgeEntry")
        if entry.status != KnowledgeStatus.OBSERVED or entry.live_use_allowed:
            raise ValueError("Refresh only permits OBSERVED entries with live use disabled")
        with self._locked():
            self._ensure()
            return self._replace_locked(entry)

    def _update(self, entry_id, change):
        with self._locked():
            self._ensure()
            original = self.path.read_bytes()
            entries = {entry.id: entry for entry in self._revisions(original.decode("utf-8"))}
            if entry_id not in entries:
                raise KeyError(entry_id)
            current = entries[entry_id]
            updated = change(current)
            updated = replace(updated, updated_at=max(utc_now(), current.updated_at),
                              live_use_allowed=False)
            return self._replace_locked(updated)

    def deduplicate(self, *, dry_run: bool = False) -> tuple[int, int]:
        """Remove duplicate physical records, retaining newest record per ID.

        The newest record is the last physical occurrence. Its created_at is
        corrected to the earliest duplicate timestamp; evidence is not merged.
        Returns (records_removed, unique_records).
        """
        with self._locked():
            self._ensure()
            revisions = self._revisions(self.path.read_bytes().decode("utf-8"))
            latest = {}
            for current in revisions:
                latest[current.id] = current
            output = []
            for current in revisions:
                if latest.get(current.id) is not current:
                    continue
                duplicates = [item for item in revisions if item.id == current.id]
                earliest = min(item.created_at for item in duplicates)
                output.append(replace(current, created_at=earliest, live_use_allowed=False))
            removed = len(revisions) - len(output)
            if removed and not dry_run:
                self._write(self._jsonl(output))
            return removed, len(output)

    def update_evidence(self, entry_id: str, evidence: str, *,
                        additional_samples: int = 0, confidence: float | None = None,
                        next_validation: str | None = None) -> KnowledgeEntry:
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("evidence must be a nonempty string")
        if type(additional_samples) is not int or additional_samples < 0:
            raise ValueError("additional_samples must be a nonnegative integer")

        def change(current):
            return replace(current, evidence=current.evidence + (evidence,),
                           sample_count=current.sample_count + additional_samples,
                           confidence=current.confidence if confidence is None else confidence,
                           next_validation=current.next_validation if next_validation is None else next_validation)

        return self._update(entry_id, change)

    def update_status(self, entry_id: str, status: KnowledgeStatus, *,
                      approved: bool = False, reason: str) -> KnowledgeEntry:
        status = KnowledgeStatus(status)
        if status == KnowledgeStatus.VALIDATED and approved is not True:
            raise ValueError("VALIDATED requires explicit approved=True")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("A status change requires a reason")

        def change(current):
            audit = f"Status {current.status.value} -> {status.value}: {reason}"
            if status == KnowledgeStatus.VALIDATED:
                audit += " [human approval: approved=True]"
            return replace(current, status=status, live_use_allowed=False,
                           evidence=current.evidence + (audit,))

        return self._update(entry_id, change)
