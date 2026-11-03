#!/usr/bin/env python
"""chain_demo.py — прогон жизненного цикла nexus_escrow на NEAR Testnet
через pure-Python RPC/подпись (tools/near_rpc.py, ноль зависимостей).

  python contracts/nexus_escrow/chain_demo.py            # весь цикл
  python contracts/nexus_escrow/chain_demo.py views      # только view-дамп

Показывает: register → create → bid (ed25519 on-chain) → accept → work →
release (rep+1) → slash (rep-1, stake burn) → escrow 0.05→0.
Паники контракта печатаются с сырым текстом — ERR_TASK_EXPIRED и т.п.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
import near_rpc  # noqa: E402

RPC = "https://test.rpc.fastnear.com"
CONTRACT = "nexus-escrow.yksunder.testnet"
ACCOUNT = "yksunder.testnet"
NEAR = 10**24


def _key() -> str:
    path = os.path.expanduser(
        f"~/.near-credentials/testnet/{ACCOUNT}.json")
    return json.load(open(path))["private_key"]


CLI = near_rpc.NearTestnetClient(endpoint=RPC, timeout_s=30)


def call(method: str, args: dict, deposit_near: float = 0.0) -> dict:
    res = CLI.send_function_call(
        ACCOUNT, CONTRACT, method, args, _key(),
        deposit_yocto=int(deposit_near * NEAR))
    print(f"    tx {res['tx_hash']}")
    for line in res["logs"]:
        print(f"    log: {line}")
    return res


def view(method: str, args: dict | None = None):
    out = CLI.view_function(CONTRACT, method, args or {})
    print(f"    {method} → {out}")
    return out


def sign_bid(task_id: str, price_yocto: int) -> str:
    seed32, pk32 = near_rpc.parse_near_private_key(_key())
    msg = f"bid:{task_id}:{ACCOUNT}:{price_yocto}".encode()
    return near_rpc.b58encode(near_rpc.ed25519_sign(msg, seed32, pk32))


def demo(resume: bool = False) -> None:
    now_ms = int(time.time() * 1000)

    if not resume:
        print("══ [1] register_agent (stake 0.1 NEAR) ══")
        pub = json.load(open(os.path.expanduser(
            f"~/.near-credentials/testnet/{ACCOUNT}.json")))["public_key"]
        call("register_agent", {"public_key": pub}, 0.1)
        view("get_agent", {"agent_id": ACCOUNT})

        print("══ [2] ERR_TASK_EXPIRED — bid на истёкшую задачу ══")
        call("create_task", {"task_id": "v2-expired", "deadline_ms": 1},
             0.05)
        try:
            call("submit_bid", {
                "task_id": "v2-expired", "price_yocto": str(2 * 10**22),
                "signature": sign_bid("v2-expired", 2 * 10**22)})
        except near_rpc.ContractCallError as e:
            print(f"    contract panic (ожидаемо): {e}")
            print(f"    failed tx: {e.tx_hash}")

    print("    refund_open_task v2-expired — эскроу возвращается")
    call("refund_open_task", {"task_id": "v2-expired"})

    print("══ [3] happy path — эскроу 0.05 → 0, reputation +1 ══")
    call("create_task", {"task_id": "v2-happy",
                         "deadline_ms": now_ms + 3_600_000}, 0.05)
    view("get_escrow_balance")
    call("submit_bid", {
        "task_id": "v2-happy", "price_yocto": str(2 * 10**22),
        "signature": sign_bid("v2-happy", 2 * 10**22)})
    call("accept_bid", {"task_id": "v2-happy", "agent_id": ACCOUNT})
    call("submit_work", {"task_id": "v2-happy",
                         "work_hash": "sha256:nexus-v2-demo"})
    call("verify_and_release", {"task_id": "v2-happy"})
    view("get_escrow_balance")
    view("get_agent", {"agent_id": ACCOUNT})

    print("══ [4] slash — дедлайн истёк, стейк сгорает, reputation -1 ══")
    call("create_task", {"task_id": "v2-slash",
                         "deadline_ms": int(time.time() * 1000) + 75_000},
         0.05)
    call("submit_bid", {
        "task_id": "v2-slash", "price_yocto": str(2 * 10**22),
        "signature": sign_bid("v2-slash", 2 * 10**22)})
    call("accept_bid", {"task_id": "v2-slash", "agent_id": ACCOUNT})
    print("    sleep 80s — ждём дедлайн...")
    time.sleep(80)
    call("slash", {"task_id": "v2-slash"})
    view("get_agent", {"agent_id": ACCOUNT})
    view("get_burned")
    view("get_contract_balance")


def views() -> None:
    for m, a in [
        ("get_burned", {}),
        ("get_escrow_balance", {}),
        ("get_contract_balance", {}),
        ("get_agent", {"agent_id": ACCOUNT}),
        ("get_task", {"task_id": "v2-happy"}),
        ("get_task", {"task_id": "v2-slash"}),
    ]:
        view(m, a)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "demo"
    if arg == "views":
        views()
    else:
        demo(resume=(arg == "continue"))
