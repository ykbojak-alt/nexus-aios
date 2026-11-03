"""generator.py — Генератор: первый ум роя.

Получает голоса 60 агентов и формулирует ПРЕДЛОЖЕНИЕ: направление,
уверенность, размер позиции, стоп-лосс. Решение не принимает —
предлагает. Возражения выносит Критик (critic.py), вердикт — Арбитр
(arbiter.py).

Score = Σ(action_value × confidence × reputation) / Σ(confidence × rep)
action_value: BUY=+1, SELL=-1, HOLD=0 → score ∈ [-1, +1].
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from .agent_swarm import SwarmVote

_VALUE = {"BUY": 1.0, "SELL": -1.0, "HOLD": 0.0}


@dataclass
class Proposal:
    """Предложение Генератора. Решение принимает Арбитр."""

    action: str            # BUY|SELL|HOLD
    score: float           # взвешенный [-1,+1]
    tally: dict            # {"BUY":n,"SELL":n,"HOLD":n}
    size_pct: float        # ≤ 2.0 — под капом Risk Manager
    stop_loss: float       # медиана стопов победившей стороны
    rationale: str

    @property
    def actionable(self) -> bool:
        return self.action in ("BUY", "SELL")


class Generator:
    def __init__(self, threshold: float = 0.6, max_position_pct: float = 2.0):
        self.threshold = threshold
        self.max_position_pct = max_position_pct

    def propose(self, votes: list[tuple[SwarmVote, float]]) -> Proposal:
        """votes: [(vote, reputation_score)] — rep приходит отдельно,
        т.к. живёт в SwarmAgent, а не в бюллетене."""
        tally = {"BUY": 0, "SELL": 0, "HOLD": 0}
        num = den = 0.0
        for v, rep in votes:
            tally[v.action] += 1
            w = v.confidence * max(rep, 0.0)
            num += _VALUE[v.action] * w
            den += w
        score = num / den if den else 0.0

        action = "BUY" if score > 0 else "SELL" if score < 0 else "HOLD"
        stop = 0.0
        if action == "BUY":
            stops = [v.stop_loss for v, _ in votes
                     if v.action == "BUY" and v.stop_loss > 0]
            stop = statistics.median(stops) if stops else 0.0
        # размер пропорционален уверенности роя, но НЕ выше капа
        size = round(min(self.max_position_pct,
                         self.max_position_pct * abs(score)), 3)
        rationale = (f"{tally['BUY']} за покупку, {tally['SELL']} за продажу, "
                     f"{tally['HOLD']} воздержались")
        return Proposal(action, score, tally, size, stop, rationale)

    def speech(self, proposal: Proposal) -> str:
        """Реплика Генератора для диалога трёх умов."""
        if proposal.action == "HOLD":
            return (f"предлагаю не торговать · score {proposal.score:+.2f} · "
                    f"{proposal.rationale}")
        stop = (f" · стоп {proposal.stop_loss:.0f}"
                if proposal.stop_loss > 0 else " · без стопа")
        return (f"предлагаю {proposal.action} · score {proposal.score:+.2f} · "
                f"размер {proposal.size_pct:.2f}%{stop} · {proposal.rationale}")
