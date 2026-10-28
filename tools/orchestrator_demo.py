"""Swarm demo: architect auctions stages, workers bid, cheapest verified bid wins.

Run: python tools/orchestrator_demo.py ["question"]

Flow: architect decomposes the query into 3 tasks (research, code, audit).
For every task all 3 workers quote a bid; the architect verifies each bid
(checksum + declared skill + sub-account shape) and awards the task to the
cheapest verified bid. Every completed task is settled by a *separate*
NearIntentPayer.pay receipt to the winner's sub-account
(`<worker>.architect.near` — sub-account naming verbatim in receipts).

stdlib-only, no network, no keys — workers and the payer are local stubs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loop_guard import LoopGuard
from near_intent_payer import NearIntentPayer

ARCHITECT = "architect"
ARCHITECT_ACCOUNT = "architect.near"
SKILLS = {"research-agent": "research", "coder": "code", "auditor": "audit"}


def sub_account(worker: str) -> str:
    """NEAR-style sub-account used verbatim in receipts: coder.architect.near."""
    return f"{worker}.{ARCHITECT_ACCOUNT}"


def _bid_checksum(worker: str, task_id: str, price: float) -> str:
    return hashlib.sha256(f"{worker}:{task_id}:{price}".encode()).hexdigest()[:16]


# ------------------------------------------------------------- workers
# Каждый воркер — локальная заглушка: bid() (котировка) + run() (работа).
# Цена детерминирована от worker+task, чтобы прогон был воспроизводимым.

def make_bid(worker: str, task_id: str) -> dict:
    base = {"research-agent": 1.10, "coder": 2.40, "auditor": 0.65}[worker]
    spread = (int(hashlib.sha256(task_id.encode()).hexdigest(), 16) % 50) / 100
    price = round(base + spread, 2)
    return {
        "worker": worker, "sub_account": sub_account(worker),
        "skill": SKILLS[worker], "task_id": task_id, "price": price,
        "checksum": _bid_checksum(worker, task_id, price),
    }


def run_research_stub(query: str) -> dict:
    """Локальная выдача research-agent: пункты + источники, без сети."""
    return {
        "skill": "research",
        "summary": f"3 пункта по запросу '{query}': определение, механика, риски",
        "sources": ["local://docs/overview.md", "local://docs/architecture.md"],
        "unverified": [],
    }


def run_coder_stub(query: str, research: dict) -> dict:
    """coder: артефакт-патч из результатов research-стадии."""
    patch = (
        f"@@ swarm-task: {query} @@\n"
        f"+ summary = {research['summary']!r}\n"
        "+ tests = ['test_outline', 'test_receipts']  # stub: 2 passed\n"
    )
    return {"skill": "code", "patch": patch, "tests_run": 2, "tests_failed": 0}


def run_auditor_stub(research: dict, code: dict) -> dict:
    """auditor: проверяет, что у research есть источники, а у code — зелёные тесты."""
    ok = bool(research.get("sources")) and code.get("tests_failed") == 0
    return {
        "skill": "audit",
        "verdict": "pass" if ok else "fail",
        "findings": [] if ok else ["нет источников или красные тесты"],
    }


WORKER_RUNS = {"research": run_research_stub, "code": run_coder_stub, "audit": run_auditor_stub}


# ------------------------------------------------------------ architect

def verify_bid(bid: dict, task: dict) -> bool:
    """Bid валиден: чексумма сходится, навык совпадает с задачей, sub-account
    имеет форму <worker>.architect.near, цена > 0."""
    return (
        bid.get("skill") == task["skill"]
        and bid.get("task_id") == task["task_id"]
        and bid.get("sub_account") == sub_account(bid.get("worker", ""))
        and bid.get("checksum") == _bid_checksum(bid.get("worker", ""), task["task_id"], bid.get("price", -1))
        and bid.get("price", 0) > 0
    )


def pick_winner(bids: list[dict], task: dict) -> dict | None:
    """Cheapest *verified* bid; список отвергнутых — в репорт честности."""
    verified = [b for b in bids if verify_bid(b, task)]
    return min(verified, key=lambda b: b["price"]) if verified else None


async def main(query: str) -> dict:
    guard = LoopGuard()
    payer = NearIntentPayer(loop_guard=guard)
    guard.require("decompose", ARCHITECT)

    tasks = [
        {"task_id": "swarm-1-research", "skill": "research",
         "desc": f"исследовать запрос: {query}"},
        {"task_id": "swarm-2-code", "skill": "code",
         "desc": "собрать артефакт из исследования"},
        {"task_id": "swarm-3-audit", "skill": "audit",
         "desc": "прогнать аудит стадий"},
    ]

    workers = ["research-agent", "coder", "auditor"]
    stage_outputs: dict[str, dict] = {}
    awards, receipts, rejected = [], [], []
    artifacts: dict[str, dict] = {}

    for task in tasks:
        guard.require("hire", ARCHITECT)
        bids = [make_bid(w, task["task_id"]) for w in workers]
        # Демонстрация верификации: подмешиваем подделанный bid с чужой чексуммой.
        if task["skill"] == "audit":
            forged = dict(bids[0], worker="research-agent", price=0.01)
            forged["checksum"] = "f" * 16
            bids.append(forged)
        rejected += [b for b in bids if not verify_bid(b, task)]

        winner = pick_winner(bids, task)
        if winner is None:
            stage_outputs[task["skill"]] = {"status": "no verified bid"}
            continue
        awards.append(winner)

        # Стадия выполняется победителем; входы берутся из уже собранных артефактов.
        runner = WORKER_RUNS[winner["skill"]]
        if winner["skill"] == "research":
            out = runner(query)
        elif winner["skill"] == "code":
            out = runner(query, artifacts.get("research") or run_research_stub(query))
        else:
            out = runner(
                artifacts.get("research") or run_research_stub(query),
                artifacts.get("code") or run_coder_stub(query, run_research_stub(query)),
            )
        artifacts[winner["skill"]] = out
        stage_outputs[task["skill"]] = {"winner": winner["worker"],
                                        "price": winner["price"], "output": out}

        # Расчёт отдельным чеком на саб-аккаунт победителя — по цене выигрышного bid.
        guard.require("verify", ARCHITECT)
        receipts.append(payer.pay(winner["sub_account"], task["task_id"], winner["price"]))

    return {
        "architect": ARCHITECT_ACCOUNT,
        "query": query,
        "tasks": [t["task_id"] for t in tasks],
        "awards": awards,
        "rejected_bids": [{"worker": b.get("worker"), "task_id": b.get("task_id"),
                           "reason": "checksum/skill/sub-account mismatch"} for b in rejected],
        "stages": stage_outputs,
        "payment_receipts": receipts,
        "audit_log": guard.audit_log,
    }


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "What is NEAR Intents?"
    print(json.dumps(asyncio.run(main(q)), ensure_ascii=False, indent=2))
