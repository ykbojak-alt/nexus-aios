"""arbiter.py — Арбитр: третий ум роя.

Слушает Генератора и Критика, выносит вердикт:

  · блокирующее возражение Критика → ОТМЕНА (HOLD, размер 0);
  · |score| < порога → шум, в кэше;
  · иначе → принято, размер и стоп из предложения.

Score Арбитр считает сам, когда получает голоса:
  score = Σ (value × confidence × weight) / Σ (confidence × weight),
где weight — дарвиновский вес агента (darwin.py): ошибся → вес упал,
оказался прав → вырос. Без голосов берётся score предложения
Генератора.

Правила пула из 100 агентов:
  · вес < 0.1 три голоса подряд — агент «мёртв», его голос не считается;
  · вес > 2.5 — вето-сила: голос говорит втрое громче (множитель ×3);
  · ≥70% тяжёлых (top-30 по весу) сказали HOLD — сделка отменяется.

Вердикт сам ничего не исполняет: правило (rule) остаётся чистой
функцией, а ордер уходит только через execute() — там Арбитр зовёт
инжектированного исполнителя (FundingExecutor → MarginPad,
FastExecutor → ccxt paper/testnet). Исполнитель по контракту:
execute_verdict(con, emit=..., equity_usd=...).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .critic import Objection
from .generator import Proposal

#: Вклад голоса в score: BUY=+1, SELL=-1, HOLD=0.
_VALUE = {"BUY": 1.0, "SELL": -1.0, "HOLD": 0.0}

#: Агент легче этого веса считается «мёртвым» для системы: его голос
#: игнорируется после DEAD_STREAK голосов подряд.
DEAD_WEIGHT = 0.1
DEAD_STREAK = 3

#: Вето-сила: агент тяжелее VETO_WEIGHT говорит втрое громче остальных.
VETO_WEIGHT = 2.5
VETO_MULTIPLIER = 3.0

#: Правило пула из 100 агентов: если ≥70% тяжёлых (top-30 по весу) сказали
#: HOLD — Арбитр обязан отменить сделку.
HEAVY_POOL_SIZE = 30
HEAVY_HOLD_RATIO = 0.7

#: Профили риска Арбитра — адаптивная агрессия. Каждый профиль задаёт
#: порог консенсуса и потолок размера позиции (% equity). Сверху профиля
#: стоят жёсткие лимиты Критика (critic.harden) — агрессия их не обходит.
RISK_PROFILES = {
    "AGGRESSIVE":   {"threshold": 0.40, "max_size_pct": 5.0},
    "BALANCED":     {"threshold": 0.55, "max_size_pct": 3.0},
    "CONSERVATIVE": {"threshold": 0.70, "max_size_pct": 1.5},
}
#: Порядок ручного переключения (горячая клавиша R в TUI).
_RISK_CYCLE = ("CONSERVATIVE", "BALANCED", "AGGRESSIVE")

#: Chaos Mode: при росте ATR больше CHAOS_ATR_GROWTH% за час обычные
#: персоны запрещены (knowledge_base), Арбитр слушает только отряд
#: шторма. Сделки такого режима несут тег risk_mode="MICRO" —
#: исполнитель открывает их микро-лотом с жёсткими TP/SL/time-exit.
CHAOS_ATR_GROWTH = 100.0
CHAOS_PERSONAS = ("ChaosScalper", "VolatilityBreakout")


@dataclass
class Consensus:
    action: str                # BUY|SELL|HOLD
    score: float               # взвешенный [-1,+1]
    tally: dict                # {"BUY":n,"SELL":n,"HOLD":n}
    amount_percent: float      # ≤ 2.0 — под капом Risk Manager
    stop_loss: float           # медиана стопов победившей стороны
    reason: str
    cancelled: bool = False    # вердикт: отменено Критиком
    objections: list[Objection] = field(default_factory=list)
    dead: list[str] = field(default_factory=list)   # «мёртвые» агенты
    risk_mode: str = "NORMAL"  # MICRO — сделка Chaos Mode (микро-лот)

    @property
    def actionable(self) -> bool:
        return self.action in ("BUY", "SELL")

    @property
    def approved(self) -> bool:
        """Вердикт APPROVED: и направление, и не отменён Критиком."""
        return self.actionable and not self.cancelled

    def speech(self) -> str:
        """Реплика Арбитра для диалога трёх умов."""
        if self.cancelled:
            return f"ОТМЕНА — {self.reason}"
        if not self.actionable:
            return f"HOLD — {self.reason}"
        return (f"принято: {self.action} · score {self.score:+.2f} · "
                f"размер {self.amount_percent:.2f}%")


class Arbiter:
    def __init__(self, threshold: float | None = None, executor=None):
        self.executor = executor
        self._dead_streaks: dict[str, int] = {}
        self._manual_profile: str | None = None
        self.risk_profile = "BALANCED"
        self.max_size_pct = RISK_PROFILES["BALANCED"]["max_size_pct"]
        # профиль задаёт дефолтный порог 0.55; явный threshold — override
        self.threshold = (RISK_PROFILES["BALANCED"]["threshold"]
                          if threshold is None else float(threshold))

    # ------------------------------------------------- профили риска
    def set_profile(self, name: str, *, manual: bool = False) -> bool:
        """Выставить профиль: порог и потолок размера. manual — пин
        ручного оверрайда (автоматика его не снимает до clear_manual)."""
        p = RISK_PROFILES.get(str(name or "").upper())
        if p is None:
            return False
        self.risk_profile = p_name = str(name).upper()
        self.threshold = p["threshold"]
        self.max_size_pct = p["max_size_pct"]
        if manual:
            self._manual_profile = p_name
        return True

    def clear_manual(self) -> None:
        """Снять ручной пин — автоматика профилей снова управляет."""
        self._manual_profile = None

    def cycle_profile(self) -> str:
        """Клавиша R: CONSERVATIVE → BALANCED → AGGRESSIVE → … (manual)."""
        nxt = _RISK_CYCLE[(_RISK_CYCLE.index(self.risk_profile) + 1)
                          % len(_RISK_CYCLE)]
        self.set_profile(nxt, manual=True)
        return nxt

    def update_profile(self, *, win_streak: int = 0, loss_streak: int = 0,
                       regime: str = "",
                       market_sentiment: float | None = None) -> str | None:
        """Авто-переход: вернёт имя нового профиля или None.

        AGGRESSIVE: win_streak ≥3 + TRENDING_UP + sentiment > 0.2.
        CONSERVATIVE: loss_streak ≥2 | sentiment < -0.5 | VOLATILE.
        Условия взаимоисключающие — порядок проверки роли не играет.
        Ручной пин (клавиша R) приоритетнее автоматики.
        """
        if self._manual_profile:
            return None
        sent = float(market_sentiment or 0.0)
        if win_streak >= 3 and regime == "TRENDING_UP" and sent > 0.2:
            want = "AGGRESSIVE"
        elif (loss_streak >= 2 or sent < -0.5
              or regime == "VOLATILE"):
            want = "CONSERVATIVE"
        else:
            want = "BALANCED"
        if want != self.risk_profile:
            self.set_profile(want)
            return want
        return None

    # --------------------------------------------------- дарвиновский score
    @staticmethod
    def effective_weight(weight: float) -> float:
        """Вес голоса с учётом вето-силы: тяжелее 2.5 → множитель ×3.

        Агент, который много раз угадал, весит больше всех остальных
        вместе взятых — но не диктатор: множитель ограничен.
        """
        w = max(float(weight or 0.0), 0.0)
        return w * VETO_MULTIPLIER if w > VETO_WEIGHT else w

    @classmethod
    def weight_mass(cls, votes) -> float:
        """Σ (confidence × weight) — суммарный вес роя."""
        return sum(v.confidence * cls.effective_weight(v.weight)
                   for v in votes)

    @classmethod
    def score_votes(cls, votes) -> float:
        """score = Σ (value × confidence × weight) / Σ (confidence × weight).

        Голос агента с большим весом (удачный) тянет score сильнее; голос
        агента тяжелее 2.5 получает вето-множитель ×3.
        """
        num = den = 0.0
        for v in votes:
            w = v.confidence * cls.effective_weight(v.weight)
            num += _VALUE.get(v.action, 0.0) * w
            den += w
        return num / den if den else 0.0

    # --------------------------------------------- мёртвые и тяжёлое большинство
    def _filter_dead(self, votes) -> tuple[list, list[str]]:
        """Отсечь «мёртвые» голоса: вес < 0.1 три раза подряд.

        Первые два голоса слабого агента в зачёт идут — он ещё жив;
        с третьего подряд его мнение системе не нужно.
        """
        alive, dead = [], []
        for v in votes:
            name = getattr(v, "agent", "") or ""
            if float(getattr(v, "weight", 1.0) or 0.0) < DEAD_WEIGHT:
                streak = self._dead_streaks.get(name, 0) + 1
                self._dead_streaks[name] = streak
                if streak >= DEAD_STREAK:
                    dead.append(name)
                    continue
            else:
                self._dead_streaks.pop(name, None)
            alive.append(v)
        return alive, dead

    @classmethod
    def heavy_hold_veto(cls, votes, top_n: int = HEAVY_POOL_SIZE,
                        ratio: float = HEAVY_HOLD_RATIO) -> str:
        """Вето тяжёлого большинства: ≥70% top-N по весу сказали HOLD.

        В пуле из 100 агентов решают не все, а тридцать самых тяжёлых —
        и если семь из десяти тяжёлых не хотят сделку, сделка не будет.
        """
        ranked = sorted(votes, key=lambda v: float(getattr(v, "weight", 1.0)
                                                   or 0.0),
                        reverse=True)[:top_n]
        if not ranked:
            return ""
        holds = sum(1 for v in ranked if v.action == "HOLD")
        share = holds / len(ranked)
        if share >= ratio:
            return (f"вето тяжёлого большинства: {holds}/{len(ranked)} "
                    f"top-{len(ranked)} по весу говорят HOLD "
                    f"({share:.0%} ≥ {ratio:.0%})")
        return ""

    @staticmethod
    def is_chaos_vote(v) -> bool:
        """Голос отряда шторма: персона агента — Chaos-семья."""
        family = str(getattr(v, "persona", "") or "").split("-")[0]
        return family in CHAOS_PERSONAS

    def rule(self, proposal: Proposal,
             objections: list[Objection] | None = None,
             votes=None, *, atr_growth: float = 0.0) -> Consensus:
        """Вердикт по предложению Генератора и возражениям Критика.

        votes — голоса роя: по ним Арбитр сам считает дарвиновский score
        (Σ confidence × weight). Без голосов берётся score предложения.
        «Мёртвые» голоса (вес < 0.1 три раза подряд) в score не идут,
        вето тяжёлого большинства (70% top-30 HOLD) отменяет сделку.

        atr_growth — рост ATR за час в %: выше CHAOS_ATR_GROWTH включает
        CHAOS_MODE — считаются только голоса Chaos-персон, обычные
        агенты (будут в HOLD) игнорируются, вето большинства снято.
        """
        objections = objections or []
        blocks = [o for o in objections if o.blocking]
        score = proposal.score
        dead: list[str] = []
        chaos = float(atr_growth or 0.0) > CHAOS_ATR_GROWTH
        alive = list(votes) if votes else []
        if votes:
            if chaos:
                alive = [v for v in alive if self.is_chaos_vote(v)]
            alive, dead = self._filter_dead(alive)
            if alive:
                score = self.score_votes(alive)

        if proposal.action == "HOLD":
            return Consensus("HOLD", score, proposal.tally, 0.0, 0.0,
                             "рой в шуме — в кэше",
                             objections=objections, dead=dead)

        if blocks:
            return Consensus("HOLD", score, proposal.tally, 0.0, 0.0,
                             f"отменено Критиком: {blocks[0].text}",
                             cancelled=True, objections=objections, dead=dead)

        if chaos and not alive:
            return Consensus("HOLD", score, proposal.tally, 0.0, 0.0,
                             "CHAOS MODE: отряд шторма молчит — "
                             "обычные голоса не в счёт",
                             objections=objections, dead=dead)

        veto = "" if chaos else (self.heavy_hold_veto(alive)
                                 if alive else "")
        if veto:
            return Consensus("HOLD", score, proposal.tally, 0.0, 0.0,
                             veto, cancelled=True, objections=objections,
                             dead=dead)

        if abs(score) < self.threshold:
            return Consensus("HOLD", score, proposal.tally, 0.0, 0.0,
                             f"|score| {abs(score):.2f} < "
                             f"{self.threshold} — шум, в кэше",
                             objections=objections, dead=dead)

        # профиль режет потолок размера: AGGRESSIVE позволяет до 5%
        size = min(proposal.size_pct, self.max_size_pct)
        return Consensus(proposal.action, score, proposal.tally,
                         size, proposal.stop_loss,
                         f"консенсус {proposal.action}: score "
                         f"{score:+.2f} ≥ {self.threshold}",
                         objections=objections, dead=dead,
                         risk_mode="MICRO" if chaos else "NORMAL")

    def execute(self, con: Consensus, executor=None, *, emit=print,
                equity_usd: float = 10_000.0, **ctx) -> dict:
        """APPROVED → ордер. Исполнитель инжектится снаружи.

        Возвращает словарь исполнителя. Отмена Критиком или HOLD до
        биржи не доходят: гейты (тень + Risk Manager) живут внутри
        исполнителя, Арбитр лишь решает, звать их или нет.
        """
        ex = executor or self.executor
        if ex is None:
            return {"status": "no_executor",
                    "reason": "исполнитель не подключён"}
        if not con.approved:
            return {"status": "skipped", "reason": con.reason}
        return ex.execute_verdict(con, emit=emit, equity_usd=equity_usd,
                                  **ctx)
