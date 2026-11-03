"""NearIntentPayer — расчёт за работу строго через nexus_escrow.

Два режима:

- ``contract`` — NEAR_ACCOUNT_ID + NEAR_ESCROW_CONTRACT заданы: все деньги
  двигаются методами смарт-контракта (create_task/submit_bid/accept_bid/
  submit_work/verify_and_release). Прямого transfer'а и fallback'а нет.
- ``stub`` — NEAR-конфига нет: локальный чек для офлайн-тестов. Сеть не
  трогается, режим честно помечен.

`pay()` в contract-режиме вызывает verify_and_release и возвращает чек с
TxHash контрактного вызова. Интент остаётся декларацией услуги
(`action: pay_for_service`), но settlement — on-chain эскроу.
"""

from __future__ import annotations

import hashlib
import json
import time

try:
    from near_rpc import EXPLORER_TX
    from near_contract_client import NearContractClient, DEFAULT_CONTRACT
    from research_agent_clients import env
except ImportError:
    from tools.near_rpc import EXPLORER_TX
    from tools.near_contract_client import NearContractClient, DEFAULT_CONTRACT
    from tools.research_agent_clients import env


class NearIntentPayer:
    def __init__(self, loop_guard=None, payer_role: str = "architect",
                 rpc=None, verbose: bool = False, mode: str | None = None,
                 contract: NearContractClient | None = None):
        self.loop_guard = loop_guard
        self.payer_role = payer_role
        self.verbose = verbose
        self.receipts: list[dict] = []
        self._rpc = rpc
        self._contract = contract
        if mode not in (None, "stub", "contract"):
            raise ValueError(f"неизвестный режим {mode!r}")
        self.mode = mode or (
            "contract" if (env("NEAR_ACCOUNT_ID")
                           and (env("NEAR_ESCROW_CONTRACT")
                                or DEFAULT_CONTRACT)) else "stub")

    @property
    def contract(self) -> NearContractClient:
        if self._contract is None:
            self._contract = NearContractClient(rpc=self._rpc)
        return self._contract

    # ------------------------------------------------ contract lifecycle
    def preflight(self) -> dict:
        return self.contract.preflight() if self.mode == "contract" else {
            "mode": "stub"}

    def ensure_agent(self, *args, **kwargs) -> dict:
        if self.mode != "contract":
            return {"step": "register_agent", "status": "stub"}
        return self.contract.ensure_agent(*args, **kwargs)

    def create_task(self, task_id: str, budget_near: float,
                    deadline_ms: int) -> dict:
        if self.loop_guard is not None:
            self.loop_guard.require("create_task", self.payer_role)
        if self.mode != "contract":
            return {"step": "create_task", "status": "stub",
                    "contract": "stub", "deposit_near": budget_near}
        return self.contract.create_task(task_id, budget_near, deadline_ms)

    def submit_bid(self, task_id: str, price_yocto: int, signature: str,
                   **kwargs) -> dict:
        if self.loop_guard is not None:
            self.loop_guard.require("submit_bid", "research-agent")
        if self.mode != "contract":
            return {"step": "submit_bid", "status": "stub",
                    "contract": "stub"}
        return self.contract.submit_bid(
            task_id, price_yocto, signature, **kwargs)

    def accept_bid(self, task_id: str, agent_id: str) -> dict:
        if self.loop_guard is not None:
            self.loop_guard.require("accept_bid", self.payer_role)
        if self.mode != "contract":
            return {"step": "accept_bid", "status": "stub",
                    "contract": "stub"}
        return self.contract.accept_bid(task_id, agent_id)

    def submit_work(self, task_id: str, work_hash: str, **kwargs) -> dict:
        if self.loop_guard is not None:
            self.loop_guard.require("submit_work", "research-agent")
        if self.mode != "contract":
            return {"step": "submit_work", "status": "stub",
                    "contract": "stub"}
        return self.contract.submit_work(task_id, work_hash, **kwargs)

    def verify_and_release(self, task_id: str) -> dict:
        if self.loop_guard is not None:
            self.loop_guard.require("verify_and_release", self.payer_role)
        if self.mode != "contract":
            return {"step": "verify_and_release", "status": "stub",
                    "contract": "stub"}
        return self.contract.verify_and_release(task_id)

    def get_task(self, task_id: str):
        return self.contract.get_task(task_id) if self.mode == "contract" else None

    def get_agent(self, account_id: str):
        return self.contract.get_agent(account_id) if self.mode == "contract" else None

    def balance_near(self, account_id: str) -> float | None:
        return (self.contract.balance_near(account_id)
                if self.mode == "contract" else None)

    # ------------------------------------------------------------- pay
    def pay(self, worker_id: str, task_id: str, amount: float,
            service: str | None = None,
            release_receipt: dict | None = None) -> dict:
        """Декларация услуги + расчёт. В contract-режиме — verify_and_release.

        release_receipt можно передать, если оркестратор уже вызвал release
        (тогда второй вызов не делается и задача не уходит в Completed дважды).
        """
        if self.loop_guard is not None:
            self.loop_guard.require("create_intent", self.payer_role)
        ts = int(time.time() * 1000)
        intent_id = "intent-" + hashlib.sha256(
            f"{worker_id}:{task_id}:{ts}".encode()).hexdigest()[:16]
        payer_account = env("NEAR_ACCOUNT_ID")
        intent = {
            "action": "pay_for_service",
            "service": service or f"{worker_id}:agent_work",
            "amount": amount,
            "currency": "NEAR",
            "intent_id": intent_id,
            "payer": payer_account or "nexus-local",
            "solver": worker_id,
            "task_id": task_id,
            "contract": (self.contract.contract_id
                         if self.mode == "contract" else None),
            "deadline_ms": ts + 300_000,
            "ts": ts,
        }
        if self.verbose:
            try:
                from ansi_demo import magenta as _mg
            except ImportError:
                from tools.ansi_demo import magenta as _mg
            print(_mg("Submitting intent to NEAR Solvers network..."))
            print(_mg(json.dumps(intent, ensure_ascii=False, indent=2)))

        receipt = {
            "intent_id": intent_id, "solver": worker_id,
            "task_id": task_id, "status": "settled", "amount": amount,
            "ts": ts, "mode": "stub", "intent": intent,
        }
        if self.mode == "stub":
            self.receipts.append(receipt)
            return receipt

        release = release_receipt or self.verify_and_release(task_id)
        receipt.update(mode="contract", contract=self.contract.contract_id,
                       contract_calls=[release])
        if release.get("status") == "success":
            receipt.update(tx_hash=release.get("tx_hash"),
                           explorer=release.get("explorer"),
                           status="settled")
        else:
            receipt.update(status="error",
                           error=release.get("error") or release.get("status"))
        self.receipts.append(receipt)
        return receipt
