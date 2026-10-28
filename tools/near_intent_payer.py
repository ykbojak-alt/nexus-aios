"""NearIntentPayer — stub emulating payment settlement via NEAR Intents.

No network, no wallet, no keys. A real integration replaces pay() with a
NEAR Intents solver call; the receipt shape stays identical.
"""

from __future__ import annotations

import hashlib
import time


class NearIntentPayer:
    def __init__(self, loop_guard=None, payer_role: str = "architect"):
        self.loop_guard = loop_guard
        self.payer_role = payer_role
        self.receipts: list[dict] = []

    def pay(self, worker_id: str, task_id: str, amount: float) -> dict:
        if self.loop_guard is not None:
            self.loop_guard.require("create_intent", self.payer_role)
        ts = int(time.time() * 1000)
        intent_id = "intent-" + hashlib.sha256(
            f"{worker_id}:{task_id}:{ts}".encode()
        ).hexdigest()[:16]
        receipt = {
            "intent_id": intent_id,
            "solver": worker_id,
            "task_id": task_id,
            "status": "settled",
            "amount": amount,
            "ts": ts,
        }
        self.receipts.append(receipt)
        return receipt
