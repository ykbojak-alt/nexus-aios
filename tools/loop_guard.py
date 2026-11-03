"""LoopGuard — policy gate between agent intent and execution.

Every command an agent wants to run passes check(); denied actions are
logged and never executed. AI judges: audit_log is the replayable evidence.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

if __package__:  # Supports both `python tools/...py` and package imports.
    from .audit_chain import append_decision, default_audit_path
else:  # pragma: no cover - exercised by direct-script imports
    from audit_chain import append_decision, default_audit_path

_POLICY_PATH = Path(__file__).resolve().parent.parent / "policies" / "command_policy.json"


class LoopGuard:
    def __init__(self, policy_path: Path | str = _POLICY_PATH,
                 audit_path: Path | str | None = None):
        self.policy = json.loads(Path(policy_path).read_text(encoding="utf-8"))
        self.audit_path = (
            Path(audit_path) if audit_path is not None else default_audit_path()
        )
        self.audit_log: list[dict] = []

    def check(self, command: str, role: str) -> dict:
        roles = self.policy["roles"]
        deny = self.policy["deny"]
        denied_explicit = command in deny or any(command.startswith(d) for d in deny)
        allowed = not denied_explicit and command in roles.get(role, {}).get("allow", [])
        verdict = "allowed" if allowed else "denied"
        decision = {
            "command": command,
            "role": role,
            "verdict": verdict,
            "ts": int(time.time() * 1000),
        }
        entry = append_decision(self.audit_path, decision)
        self.audit_log.append(entry)
        return entry

    def require(self, command: str, role: str) -> dict:
        """Raise PermissionError on denial; returns the audit entry otherwise."""
        entry = self.check(command, role)
        if entry["verdict"] == "denied":
            raise PermissionError(f"LoopGuard denied '{command}' for role '{role}'")
        return entry
