"""epoch_rewarder.py — расчёты эпохи: PnL-атрибуция, репутация, NEAR.

Блокчейн — только здесь и только раз в эпоху: награда лучшим агентам
идёт через nexus-escrow (create_task→bid→accept→work→release).
60 виртуальных агентов НЕ имеют NEAR-аккаунтов — сеттлмент платит
зарегистрированному on-chain оператору роя (NEAR_AGENT_ID). Локальная
репутация хранится в swarm/reputation.json.

Без NEAR-креденшелов — честный stub: TxHash помечен `stub-*`,
ничего не маскируется под реальную выплату.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .agent_swarm import SwarmAgent, SwarmVote

REP_PATH = Path(__file__).resolve().parent / "reputation.json"


def load_reputation(path: Path = REP_PATH) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_reputation(rep: dict, path: Path = REP_PATH) -> None:
    path.write_text(json.dumps(rep, indent=2, ensure_ascii=False),
                    encoding="utf-8")


def ensure_reputation(names, path: Path = REP_PATH,
                      score: float = 1.0) -> dict:
    """Дописать недостающие записи репутации (пул из 100 агентов).

    Новый агент, которого система ещё не знает, стартует с репутацией
    1.0 — как и его дарвиновский вес. Файл хранит сколько угодно
    записей: 100 агентов — 100 строк.
    """
    rep = load_reputation(path)
    added = False
    for name in names or []:
        if name not in rep:
            rep[name] = float(score)
            added = True
    if added:
        save_reputation(rep, path)
    return rep


class EpochRewarder:
    """Атрибуция PnL по голосам и обновление reputation_score."""

    def __init__(self, rep_path: Path = REP_PATH):
        self.rep_path = rep_path

    # --------------------------------------------------- pnl/rep
    def attribute_pnl(self, agents: list[SwarmAgent],
                      votes: list[SwarmVote],
                      entry_price: float, exit_price: float) -> dict:
        """Голос = гипотетическая сделка: BUY зарабатывает на росте,
        SELL — на падении, HOLD — ноль. PnL ∝ confidence × 1% от 10k."""
        ret = (exit_price - entry_price) / entry_price
        value = {"BUY": 1.0, "SELL": -1.0, "HOLD": 0.0}
        for v in votes:
            for a in agents:
                if a.name == v.agent:
                    a.pnl_usdt += (value[v.action] * v.confidence
                                   * ret * 10_000 * 0.01)
                    break
        return {a.name: round(a.pnl_usdt, 4) for a in agents}

    def update_reputation(self, agents: list[SwarmAgent],
                          lr: float = 0.5) -> dict:
        """rep *= (1 + lr × norm_pnl); clamp [0.1, 3.0]; persist JSON."""
        pnl = {a.name: a.pnl_usdt for a in agents}
        mx = max((abs(x) for x in pnl.values()), default=0.0) or 1.0
        rep = load_reputation(self.rep_path)
        for a in agents:
            old = rep.get(a.name, a.reputation_score)
            norm = pnl[a.name] / mx            # -1..+1
            a.reputation_score = round(
                min(3.0, max(0.1, old * (1 + lr * norm))), 4)
            rep[a.name] = a.reputation_score
        save_reputation(rep, self.rep_path)
        return rep

    # --------------------------------------------------- on-chain
    def settle_onchain(self, top_agents: list[str],
                       reward_near: float = 0.05,
                       epoch_id: str | None = None) -> dict:
        """Выплата пулу роя через nexus-escrow. Реальный путь — полный
        жизненный цикл контракта; без креденшелов — stub."""
        epoch_id = epoch_id or f"epoch-{int(time.time())}"
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve()
                                   .parents[2].parent))
            from tools.near_contract_client import (
                NearContractClient, near_to_yocto, sign_bid)
            from tools.research_agent_clients import env
            c = NearContractClient()
            c.preflight()          # упадёт без ключей/контракта → stub
            task_id = f"swarm-{epoch_id}"[:64]
            deadline = int(time.time() * 1000) + 86_400_000   # +24ч
            res = {"task": task_id, "agents": top_agents, "txs": {}}
            res["txs"]["create_task"] = c.create_task(
                task_id, reward_near, deadline)["tx_hash"]
            agent_id = c.agent_account
            price_y = near_to_yocto(reward_near)
            sig = sign_bid(c.agent_key, task_id, agent_id,
                           price_y)["signature"]
            res["txs"]["submit_bid"] = c.submit_bid(
                task_id, price_y, sig)["tx_hash"]
            res["txs"]["accept_bid"] = c.accept_bid(
                task_id, agent_id)["tx_hash"]
            res["txs"]["submit_work"] = c.submit_work(
                task_id, f"epoch:{','.join(top_agents[:5])}"[:200]
            )["tx_hash"]
            res["txs"]["verify_and_release"] = \
                c.verify_and_release(task_id)["tx_hash"]
            res["mode"] = "onchain"
            return res
        except Exception as e:  # noqa: BLE001 — honest stub
            return {"mode": "stub", "reason": str(e),
                    "task": epoch_id, "agents": top_agents,
                    "reward_near": reward_near,
                    "txs": {"stub": f"stub-{epoch_id}-"
                             f"{int(time.time()) % 10**6:06d}"}}
