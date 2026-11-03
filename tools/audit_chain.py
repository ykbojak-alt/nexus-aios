"""Persistent SHA-256 hash-chained JSONL audit log for LoopGuard."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from pathlib import Path
from typing import Mapping

GENESIS_HASH = "0" * 64
_HASH_INPUT_FIELDS = ("role", "command", "verdict", "ts", "prev_hash")
_RECORD_FIELDS = frozenset((*_HASH_INPUT_FIELDS, "hash"))
_WRITE_LOCK = threading.Lock()


class AuditChainError(ValueError):
    """Raised when an audit chain is malformed, altered, or cannot be extended."""


def default_audit_path() -> Path:
    """Return the persistent user-local path, overridable for deployments/tests."""
    configured = os.environ.get("NEXUS_LOOPGUARD_AUDIT_PATH")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".nexus" / "audit" / "loop_guard.jsonl"


def _hash_record(record: Mapping[str, object]) -> str:
    payload = {field: record[field] for field in _HASH_INPUT_FIELDS}
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _validate_record(record: object, line_number: int,
                     expected_prev_hash: str) -> dict[str, object]:
    if not isinstance(record, dict) or set(record) != _RECORD_FIELDS:
        raise AuditChainError(f"invalid audit record fields at line {line_number}")
    if not all(isinstance(record[field], str)
               for field in ("role", "command", "verdict", "prev_hash", "hash")):
        raise AuditChainError(f"invalid audit record types at line {line_number}")
    if not isinstance(record["ts"], int) or isinstance(record["ts"], bool):
        raise AuditChainError(f"invalid audit timestamp at line {line_number}")
    if not hmac.compare_digest(record["prev_hash"], expected_prev_hash):
        raise AuditChainError(f"broken audit link at line {line_number}")
    expected_hash = _hash_record(record)
    if not hmac.compare_digest(record["hash"], expected_hash):
        raise AuditChainError(f"audit hash mismatch at line {line_number}")
    return record


def _read_and_verify(path: Path) -> tuple[str, int]:
    if not path.exists():
        return GENESIS_HASH, 0
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AuditChainError(f"cannot read audit log: {exc}") from exc
    if raw and not raw.endswith(b"\n"):
        raise AuditChainError("audit log has an incomplete final line")

    previous_hash = GENESIS_HASH
    line_count = 0
    for line_count, raw_line in enumerate(raw.splitlines(), start=1):
        try:
            line = raw_line.decode("utf-8")
            record = json.loads(line, object_pairs_hook=_strict_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise AuditChainError(
                f"invalid audit JSON at line {line_count}"
            ) from exc
        verified = _validate_record(record, line_count, previous_hash)
        previous_hash = str(verified["hash"])
    return previous_hash, line_count


def _exclusive_file_lock(lock_path: Path):
    """Context manager holding a cross-process lock on the audit log sidecar."""
    class FileLock:
        def __enter__(self):
            self.handle = lock_path.open("a+b")
            if self.handle.seek(0, os.SEEK_END) == 0:
                self.handle.write(b"\0")
                self.handle.flush()
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            try:
                self.handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()

    return FileLock()


def append_decision(path: str | Path, decision: Mapping[str, object]) -> dict[str, object]:
    """Verify and append one decision, refusing to extend a damaged chain."""
    audit_path = Path(path).expanduser()
    required = {"role", "command", "verdict", "ts"}
    if set(decision) != required:
        raise AuditChainError("decision must contain role, command, verdict, and ts")
    if not all(isinstance(decision[field], str)
               for field in ("role", "command", "verdict")):
        raise AuditChainError("decision role, command, and verdict must be strings")
    if not isinstance(decision["ts"], int) or isinstance(decision["ts"], bool):
        raise AuditChainError("decision timestamp must be an integer")

    audit_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = audit_path.with_name(audit_path.name + ".lock")
    with _WRITE_LOCK, _exclusive_file_lock(lock_path):
        previous_hash, _ = _read_and_verify(audit_path)
        record: dict[str, object] = {
            "role": decision["role"],
            "command": decision["command"],
            "verdict": decision["verdict"],
            "ts": decision["ts"],
            "prev_hash": previous_hash,
        }
        record["hash"] = _hash_record(record)
        encoded = (json.dumps(record, ensure_ascii=False,
                              separators=(",", ":")) + "\n").encode("utf-8")
        try:
            with audit_path.open("ab") as audit_file:
                audit_file.write(encoded)
                audit_file.flush()
                os.fsync(audit_file.fileno())
        except OSError as exc:
            raise AuditChainError(f"cannot append audit log: {exc}") from exc
        return record


def verify_chain(path: str | Path) -> bool:
    """Return True for a valid (or not-yet-created) chain; raise on tampering."""
    _read_and_verify(Path(path).expanduser())
    return True
