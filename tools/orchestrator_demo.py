"""Swarm auction on-chain: задача → эскроу → bids → работа → release.

Run: python tools/orchestrator_demo.py ["question"]

Контракт: nexus_escrow (default nexus-escrow.yksunder.testnet). Деньги
двигаются ТОЛЬКО методами контракта через tools/near_rpc.py:

  1. register_agent (если агент ещё не в реестре) — стейк 1 NEAR;
  2. create_task — менеджер блокирует бюджет 0.05 NEAR;
  3. submit_bid — research-agent подписывает bid своим ed25519-ключом;
     freelancer присылает bid со сломанной подписью — контракт отвергает
     его на on-chain проверке;
  4. accept_bid — менеджер фиксирует победителя;
  5. submit_work — победитель сдаёт sha256 результата;
  6. verify_and_release — контракт платит победителю, остаток возвращает.

Цены детерминированы: base_price + len(prompt) * 0.0001. research-agent
всегда на 12% дешевле analyst.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loop_guard import LoopGuard
from near_intent_payer import NearIntentPayer
from near_rpc import b58decode, b58encode, ed25519_verify, parse_near_private_key

ARCHITECT = "architect"
ARCHITECT_ACCOUNT = "architect.near"
DEFAULT_BUDGET_NEAR = 0.05
SKILL = "research"
DEADLINE_SECONDS = 600

BASE_PRICES = {"research-agent": 0.018,
               "analyst": 0.035,
               "freelancer": 0.010}
RESEARCH_DISCOUNT = 0.12  # research всегда на 12% дешевле analyst

WORKER_RUNS: dict[str, object] = {}


def make_task_id(query: str) -> str:
    stamp = time.strftime("%H%M%S")
    digest = hashlib.sha256(f"{query}:{time.time_ns()}".encode()).hexdigest()[:6]
    return f"auction-{stamp}-{digest}"


def quote_price(worker: str, prompt: str) -> float:
    """Детерминированная котировка: base_price + len(prompt) * 0.0001.

    Для analyst цена выводится из research-котировки так, чтобы research
    оставался ровно на RESEARCH_DISCOUNT дешевле при любой длине запроса.
    """
    if worker == "analyst":
        return round(quote_price("research-agent", prompt)
                     / (1.0 - RESEARCH_DISCOUNT), 6)
    return round(BASE_PRICES[worker] + len(prompt) * 0.0001, 6)


def price_yocto(price_near: float) -> int:
    return int(round(price_near * 10**24))


def _local_demo_key(worker: str) -> str:
    """Детерминированный demo-ключ для офлайн-аукциона (не для сети)."""
    seed = hashlib.sha256(f"nexus-demo:{worker}".encode()).digest()
    from near_rpc import ed25519_publickey
    return "ed25519:" + b58encode(seed + ed25519_publickey(seed))


def _sign(worker: str, task_id: str, account: str, price_yocto: int,
          private_key: str, broken: bool = False) -> dict:
    try:
        from near_contract_client import sign_bid, sign_bid_broken
    except ImportError:
        from tools.near_contract_client import sign_bid, sign_bid_broken
    signer = sign_bid_broken if broken else sign_bid
    return signer(private_key, task_id, account, price_yocto)


def make_bid(worker: str, task: dict, prompt: str, *,
             account: str | None = None, private_key: str | None = None,
             broken: bool = False, local: bool = False) -> dict:
    price = quote_price(worker, prompt)
    yocto = price_yocto(price)
    bid = {
        "worker": worker, "account": account, "skill": SKILL,
        "task_id": task["task_id"], "price": price, "price_yocto": yocto,
        "local": local,
    }
    if local or not private_key:
        bid.update(signature=None, public_key=None)
        return bid
    signed = _sign(worker, task["task_id"], account or worker, yocto,
                   private_key, broken=broken)
    bid.update(signature=signed["signature"],
               public_key=signed["public_key"])
    return bid


def verify_bid(bid: dict, task: dict,
               registered_public_key: str | None = None) -> str | None:
    """None — bid валиден. Иначе причина отклонения.

    Подпись проверяется ed25519 по ключу из реестра контракта (для
    freelancer это тот же аккаунт-релей research-bot, но подпись — от
    сломанного ключа, поэтому математика её отвергает).
    """
    if bid.get("skill") != task["skill"]:
        return "skill mismatch"
    if bid.get("task_id") != task["task_id"]:
        return "task_id mismatch"
    price = bid.get("price", 0)
    if not isinstance(price, (int, float)) or price <= 0:
        return "non-positive price"
    if price > task["budget"]:
        return "over budget"
    if bid.get("local"):
        return None
    signature, public_key = bid.get("signature"), bid.get("public_key")
    if not signature or not public_key:
        return "unsigned bid"
    pk = registered_public_key or public_key
    msg = f"bid:{task['task_id']}:{bid['account']}:{bid['price_yocto']}".encode()
    try:
        ok = ed25519_verify(
            b58decode(signature),
            msg,
            b58decode(pk.split(":", 1)[-1]))
    except Exception:  # noqa: BLE001 — битая кодировка = невалидная подпись
        ok = False
    return None if ok else "ed25519_verify failed"


def pick_winner(bids: list[dict], task: dict,
                registered_public_key: str | None = None) -> dict | None:
    verified = [b for b in bids
                if verify_bid(b, task, registered_public_key) is None]
    return min(verified, key=lambda b: b["price"]) if verified else None


# ------------------------------------------------------------- workers

def run_research_stub(query: str) -> dict:
    return {
        "skill": "research", "mode": "stub", "verified": True,
        "summary": f"3 пункта по запросу '{query}': определение, механика, риски",
        "sources": ["local://docs/overview.md", "local://docs/architecture.md"],
        "unverified": [],
    }


def run_analyst_stub(query: str) -> dict:
    return {
        "skill": "research", "mode": "stub", "verified": True,
        "summary": f"analyst-вывод по '{query}': оценка без живого поиска",
        "sources": [], "unverified": [], "confidence": 0.6,
    }


def _run_research_stage(query: str, agent=None) -> dict:
    try:
        import research_agent_demo as ra_demo
    except ImportError:
        try:
            from tools import research_agent_demo as ra_demo  # noqa: N813
        except ImportError:
            return run_research_stub(query)
    if agent is None and not ra_demo.live_configured():
        return run_research_stub(query)
    try:
        res = ra_demo.run_research_loop(query, agent=agent)
    except Exception as e:  # noqa: BLE001 — сырой сбой виден судьям
        return {"skill": "research", "mode": "live_error", "error": str(e),
                "verified": False}
    if res.get("mode") != "live":
        return run_research_stub(query)
    return {
        "skill": "research", "mode": "live",
        "summary": res["answer"],
        "sources": [s["url"] for s in res["sources"]],
        "unverified": res["unverified"], "verified": res["verified"],
        "url_status": res.get("url_status", {}),
        "iterations": res["iterations"],
    }


# ------------------------------------------------------------ auction

def _contract_log(step: str, receipt: dict) -> str:
    if receipt.get("status") == "stub":
        return f"[contract] {step:<20} local stub (offline)"
    if receipt.get("status") == "already_registered":
        stake = receipt.get("stake_near")
        stake_txt = f"{stake:.6f} NEAR" if isinstance(stake, (int, float)) else "—"
        return (f"[contract] {step:<20} already registered "
                f"(stake={stake_txt}, key={receipt.get('public_key') or '—'}) "
                f"→ {receipt.get('contract', '')}")
    line = (f"[contract] {step:<20} TxHash: {receipt.get('tx_hash', '—')} "
            f"→ {receipt.get('contract', '')}")
    if receipt.get("status") == "failed":
        line += f" — FAILED: {receipt.get('error', '')}"
    return line


async def run_auction(query: str, budget: float = DEFAULT_BUDGET_NEAR,
                      payer: "NearIntentPayer | None" = None,
                      guard: "LoopGuard | None" = None,
                      agent=None, emit=None) -> dict:
    guard = guard or LoopGuard()
    payer = payer or NearIntentPayer(loop_guard=guard)
    lines: list[str] = []
    contract_receipts: list[dict] = []

    def say(line: str) -> None:
        if emit is not None:
            emit(line)
        else:
            lines.append(line)

    def record(receipt: dict) -> None:
        if receipt.get("status") != "stub":
            contract_receipts.append(receipt)
        say(_contract_log(receipt.get("step", "?"), receipt))

    task_id = make_task_id(query)
    guard.require("decompose", ARCHITECT)
    task = {"task_id": task_id, "skill": SKILL, "budget": budget,
            "desc": f"исследовать запрос: {query}"}

    contract_mode = payer.mode == "contract"
    manager_account = (payer.contract.manager_account if contract_mode
                       else ARCHITECT_ACCOUNT)
    agent_account = (payer.contract.agent_account if contract_mode
                     else "research-bot.testnet")

    if contract_mode:
        pre = payer.preflight()
        say(f"[contract] {pre['contract']} deployed, "
            f"balance={pre['contract_balance_near']:.6f} NEAR")
        record(payer.ensure_agent())
    say(f"[auction] менеджер {manager_account} объявил задачу {task_id} "
        f"— бюджет {budget:.3f} NEAR")

    guard.require("hire", ARCHITECT)
    record(payer.create_task(
        task_id, budget, int(time.time() * 1000) + DEADLINE_SECONDS * 1000))

    # research-ключ: в contract-режиме — реальный ключ аккаунта research-bot;
    # офлайн — детерминированный demo-ключ (сеть не трогается).
    research_key = payer.contract.agent_key if contract_mode else _local_demo_key(
        "research-agent")
    _seed, research_pub = parse_near_private_key(research_key)
    registered_pk = "ed25519:" + b58encode(research_pub)

    research_bid = make_bid("research-agent", task, query, account=agent_account,
                            private_key=research_key)
    analyst_bid = make_bid("analyst", task, query,
                           account="analyst.testnet", local=True)
    freelancer_bid = make_bid("freelancer", task, query, account=agent_account,
                              private_key=research_key, broken=True)

    # valid bid — on-chain (contract) или локально (offline)
    if contract_mode:
        rb = payer.submit_bid(task_id, research_bid["price_yocto"],
                              research_bid["signature"])
        research_bid["contract_tx"] = rb.get("tx_hash")
        record(rb)
    reason = verify_bid(research_bid, task, registered_pk)
    say(f"[bid] research-agent   {research_bid['price']:.4f} NEAR — "
        f"{'verified' if reason is None else 'REJECTED (' + reason + ')'}")

    # freelancer: сломанная подпись. В contract-режиме отправляем реально —
    # контракт обязан отвергнуть её ed25519-проверкой.
    if contract_mode:
        fb = payer.submit_bid(task_id, freelancer_bid["price_yocto"],
                              freelancer_bid["signature"], allow_failure=True)
        freelancer_bid["contract_tx"] = fb.get("tx_hash")
        record(fb)
        contract_rejection = fb.get("error") if fb.get("status") == "failed" else ""
    else:
        contract_rejection = ""
    reason = verify_bid(freelancer_bid, task, registered_pk)
    detail = reason or "unexpectedly verified"
    if contract_rejection:
        detail = f"ed25519_verify failed: {contract_rejection}"
    say(f"[bid] freelancer       {freelancer_bid['price']:.4f} NEAR — "
        f"REJECTED ({detail})")

    reason = verify_bid(analyst_bid, task, registered_pk)
    say(f"[bid] analyst          {analyst_bid['price']:.4f} NEAR — "
        f"{'local quote (not registered on-chain)' if reason is None else 'REJECTED'}")

    bids = [research_bid, analyst_bid, freelancer_bid]
    rejected = []
    for b in bids:
        r = verify_bid(b, task, registered_pk)
        if r is not None:
            rej = {"worker": b["worker"], "task_id": task_id,
                   "price": b["price"], "reason": r}
            if b is freelancer_bid and contract_rejection:
                rej["contract_error"] = contract_rejection
            rejected.append(rej)
    winner = pick_winner(bids, task, registered_pk)
    if winner is None:
        say("[auction] ни одного верифицированного bid — задача не выполнена")
        return {
            "architect": manager_account, "query": query, "task": task,
            "bids": bids, "rejected_bids": rejected, "winner": None,
            "output": None, "payment_receipts": [],
            "contract_receipts": contract_receipts,
            "audit_log": guard.audit_log, "auction_log": lines,
        }
    say(f"[auction] победитель: {winner['worker']} @ {winner['price']:.4f} NEAR")
    record(payer.accept_bid(task_id, winner["account"]))

    worker = winner["worker"]
    if worker == "research-agent":
        say("[work] research-agent: Plan→Act→Observe "
            "(live при ключах, иначе stub)")
        output = _run_research_stage(query, agent=agent)
    else:
        say(f"[work] {worker}: desk-анализ (stub)")
        output = run_analyst_stub(query)
    say(f"[work] {worker} завершил: sources={len(output.get('sources', []))} "
        f"mode={output.get('mode', '—')} verified={output.get('verified')}")

    if output.get("verified") is False:
        say("[verify] ответ не прошёл URL-проверку — release не выполняется")
        return {
            "architect": manager_account, "query": query, "task": task,
            "bids": bids, "rejected_bids": rejected, "winner": winner,
            "output": output, "payment_receipts": [],
            "contract_receipts": contract_receipts,
            "audit_log": guard.audit_log, "auction_log": lines,
        }

    answer = output.get("summary") or json.dumps(output, ensure_ascii=False)
    work_hash = "sha256:" + hashlib.sha256(answer.encode("utf-8")).hexdigest()
    record(payer.submit_work(task_id, work_hash))

    guard.require("verify", ARCHITECT)
    receipt = payer.pay(winner["account"], task_id, winner["price"],
                        service="Tavily_Search")
    for call in receipt.get("contract_calls", []):
        record(call)
    say(f"[settle] intent {receipt['intent_id']} → "
        f"status={receipt['status']} mode={receipt['mode']}")

    return {
        "architect": manager_account, "query": query, "task": task,
        "bids": bids, "rejected_bids": rejected, "winner": winner,
        "output": output, "payment_receipts": [receipt],
        "contract_receipts": contract_receipts,
        "contract": payer.contract.contract_id if contract_mode else "stub",
        "work_hash": work_hash,
        "audit_log": guard.audit_log, "auction_log": lines,
    }


async def main(query: str) -> dict:
    return await run_auction(query)


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "What is NEAR Intents?"
    print(json.dumps(asyncio.run(main(q)), ensure_ascii=False, indent=2))
