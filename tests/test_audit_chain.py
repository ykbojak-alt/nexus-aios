import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.loop_guard import LoopGuard


class AuditChainTests(unittest.TestCase):
    def test_loopguard_persists_hash_linked_decisions_as_jsonl(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.jsonl"
            guard = LoopGuard(audit_path=audit_path)

            first = guard.check("read_market", "researcher")
            second = guard.check("sign_tx", "researcher")

            rows = [json.loads(line) for line in audit_path.read_text(
                encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(
                list(rows[0]),
                ["role", "command", "verdict", "ts", "prev_hash", "hash"],
            )
            self.assertEqual(rows[0]["role"], "researcher")
            self.assertEqual(rows[0]["command"], "read_market")
            self.assertIsInstance(rows[0]["ts"], int)
            self.assertEqual(rows[0]["prev_hash"], "0" * 64)
            self.assertEqual(rows[1]["prev_hash"], rows[0]["hash"])
            self.assertEqual(first["hash"], rows[0]["hash"])
            self.assertEqual(second["hash"], rows[1]["hash"])

            from tools.audit_chain import verify_chain

            self.assertTrue(verify_chain(audit_path))

    def test_verifier_detects_tampered_record_and_append_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.jsonl"
            guard = LoopGuard(audit_path=audit_path)
            guard.check("read_market", "researcher")
            guard.check("sign_tx", "researcher")
            guard.check("create_intent", "architect")

            rows = [json.loads(line) for line in audit_path.read_text(
                encoding="utf-8").splitlines()]
            rows[1]["verdict"] = "allowed"
            audit_path.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                encoding="utf-8",
            )

            from tools.audit_chain import AuditChainError, verify_chain

            with self.assertRaisesRegex(AuditChainError, "line 2"):
                verify_chain(audit_path)
            with self.assertRaises(AuditChainError):
                guard.check("read_market", "researcher")
            self.assertEqual(len(audit_path.read_text(
                encoding="utf-8").splitlines()), 3)


if __name__ == "__main__":
    unittest.main()
