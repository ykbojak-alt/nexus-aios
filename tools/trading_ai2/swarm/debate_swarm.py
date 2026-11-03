"""debate_swarm.py — Swarm of Swarms: иерархический парламент.

Плоское голосование 12 агентов заменено тремя мини-роями по 4 агента с
внутренними дебатами в два раунда:

  Technical Swarm: TrendFollower, MeanReversion, Scalper, Breakout
  Sentiment Swarm: Analyst, Contrarian, Volatility, RsiWatcher
  Risk Swarm:      RiskManager, StopHunter, Hodler, MacdTrader

Раунд 1 — каждый агент голосует независимо (свой LLM-вызов). Раунд 2 —
агент получает ответы трёх коллег из мини-роя и может изменить голос
или привести контраргумент (текст уходит в user-JSON ключом «debate» —
модель видит его как часть снимка). Консенсус четвёрки — тот же
взвешенный score, что у Арбитра (Σ conf × weight).

Раунд 1 остаётся при вердикте (SwarmVerdict.opening): переходы второго
раунда уходят в историю как данные, и окно «Решение» показывает «BUY →
HOLD» голосом участника, а не только строками дерева дебатов.

Правила Мета-Арбитра:
  · 3 вердикта мини-роев становятся мета-голосами (weight=1.0) и идут в
    обычный arb.rule — Weighted Score уже встроен;
  · мини-рой без консенсуса (2 на 2 или |score| < 0.05) = HOLD 0.0 —
    голос нулевого веса, на мета-score не влияет;
  · Risk Swarm HOLD с confidence > 0.7 — абсолютное вето.

Модели: внутри каждой четвёрки обязаны 4 разные LLM — ростеру
назначаются model_id из пул-плана подряд, чтобы агенты не дебатировали
на одной архитектуре.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

try:
    from .agent_swarm import SwarmVote, _model_label
    from .arbiter import Arbiter
except ImportError:  # pragma: no cover — запуск без пакета
    from swarm.agent_swarm import SwarmVote, _model_label
    from swarm.arbiter import Arbiter

#: Парламент: мини-рой → персоны четвёрки. Порядок dict — порядок
#: печати блоков и порядок мета-голосов.
MINI_SWARMS: dict[str, tuple[str, ...]] = {
    "Technical": ("TrendFollower", "MeanReversion", "Scalper", "Breakout"),
    "Sentiment": ("Analyst", "Contrarian", "Volatility", "RsiWatcher"),
    "Risk": ("RiskManager", "StopHunter", "Hodler", "MacdTrader"),
}

#: Паттерн дебатов: текст коллег встраивается в snap → попадает в
#: user-JSON второго раунда.
DEBATE_RULE = (
    "Вот что говорят твои коллеги по мини-рою: {peers}. Если ты "
    "согласен с их аргументами — измени свой голос и скажи об этом в "
    "reason. Если нет — приведи контраргумент в reason и оставь свой "
    "голос.")

#: Мини-рой ниже этого score считается без консенсуса → HOLD 0.0.
DEADLOCK_SCORE = 0.05
#: Абсолютное вето Risk-роя: HOLD при confidence выше порога.
RISK_VETO_CONF = 0.7


@dataclass
class SwarmVerdict:
    """Итог дебатов одного мини-роя — вход Мета-Арбитра."""
    name: str                    # Technical|Sentiment|Risk
    action: str                  # BUY|SELL|HOLD
    score: float                 # взвешенный [-1,+1]
    confidence: float            # |score| — 0.0 при deadlock
    members: list = field(default_factory=list)  # финальные голоса
    #: Голоса раунда 1 — непустые только если второй раунд реально был.
    #: По ним видно, кого дебаты переубедили; при единогласном первом
    #: раунде спорить было не о чем и отсчётной точки нет.
    opening: list = field(default_factory=list)

    def to_vote(self) -> SwarmVote:
        """Вердикт → мета-голос: weight=1.0, score Арбитра посчитает
        сам по confidence — deadlock (conf 0) на итог не влияет."""
        return SwarmVote(action=self.action,
                         confidence=round(self.confidence, 3),
                         reason=f"{self.name} Swarm consensus",
                         stop_loss=0.0, agent=f"{self.name}Swarm",
                         persona=f"{self.name}Swarm",
                         mode="debate", weight=1.0)


def pick_roster(pool, chaos: bool = False) -> dict:
    """Ростер парламента: лучший по весу агент каждой нужной персоны.

    Пул — 100 слотов и 15 семей; дебатируют 12 (по одному на семью
    из MINI_SWARMS). Отсутствующая семья — честный пропуск слота, а
    не подмена другой персоной.

    chaos=True (ATR +100%/час): парламент уступает место отряду
    шторма — дебатируют только ChaosScalper и VolatilityBreakout,
    их голоса уйдут в Арбитр напрямую (CHAOS_PERSONAS), минуя мету.
    """
    if chaos:
        crew = []
        for fam in ("ChaosScalper", "VolatilityBreakout"):
            cand = [a for a in pool.agents
                    if str(getattr(a, "persona", "")) == fam]
            if cand:
                crew.append(max(cand, key=lambda a: a.weight))
        return {"Chaos": crew}
    roster: dict[str, list] = {}
    for swarm_name, families in MINI_SWARMS.items():
        crew = []
        for fam in families:
            cand = [a for a in pool.agents
                    if str(getattr(a, "persona", "")) == fam]
            if cand:
                crew.append(max(cand, key=lambda a: a.weight))
        roster[swarm_name] = crew
    return roster


def assign_models(pool, roster: dict) -> None:
    """4 разные LLM внутри каждой четвёрки: model_id идут подряд из
    ген-плана пула (при ≥4 моделях гарантирует различность)."""
    models = list(getattr(pool, "_gen_models", []) or [])
    if not models:
        return
    for swarm_name, crew in roster.items():
        for i, ag in enumerate(crew):
            model = models[i % len(models)]
            if model and getattr(ag, "model_id", "") != model:
                ag.model_id = model
                ag.llm = pool._llm_for(model)


def _peers_text(votes: list, me: str) -> str:
    """«коллега: ACTION conf (reason)» — ответы остальных членов роя."""
    parts = []
    for v in votes:
        if v.agent == me:
            continue
        parts.append(f"{v.persona}: {v.action} {v.confidence:.2f} "
                     f"({v.reason[:120]})")
    return "; ".join(parts)


def _stub_round2(vote: SwarmVote, peers: list) -> SwarmVote:
    """Дебаты без LLM: детерминированная уступка сильному большинству.

    ≥2 коллег уверенно (conf ≥ 0.5) направленно против — агент уходит в
    HOLD и ссылается на их аргумент; ≥2 коллег с ним — confidence растёт.
    """
    opp = [p for p in peers
           if p.action not in (vote.action, "HOLD")
           and p.confidence >= 0.5]
    if len(opp) >= 2 and vote.action != "HOLD":
        why = opp[0].reason[:80]
        return vote.model_copy(update={
            "action": "HOLD", "confidence": min(vote.confidence, 0.5),
            "reason": f"согласен с коллегами: {why}"})
    same = [p for p in peers
            if p.action == vote.action and p.confidence >= 0.5]
    if len(same) >= 2 and vote.action != "HOLD":
        return vote.model_copy(update={
            "confidence": min(1.0, vote.confidence + 0.1),
            "reason": vote.reason})
    return vote


def _debate_round2(ag, snap: dict, round1: list) -> SwarmVote:
    """Второй раунд: агент видит голоса коллег и может изменить свой."""
    peers_txt = _peers_text(round1, ag.name)
    own = next((v for v in round1 if v.agent == ag.name), None)
    if ag.llm and getattr(ag.llm, "live", False):
        ctx = {**snap, "debate": DEBATE_RULE.format(peers=peers_txt),
               "round": 2, "your_round1":
                   (f"{own.action} {own.confidence:.2f}"
                    if own else "—")}
        return ag.decide(ctx)
    peers = [v for v in round1 if v.agent != ag.name]
    if own is None:
        return SwarmVote(action="HOLD", confidence=0.0,
                         reason="нет первичного голоса", stop_loss=0.0,
                         agent=ag.name, persona=ag.persona,
                         mode="stub", weight=ag.weight)
    return _stub_round2(own, peers)


def _consensus(name: str, votes: list) -> SwarmVerdict:
    """Взвешенный score четвёрки; ничья направлений или |s|<0.05 →
    HOLD 0.0 (голос нулевого веса для Мета-Арбитра).

    HOLD-большинство — не ничья: четвёрка уверенно стоит в стороне, и её
    confidence — средневзвешенная уверенность удержавшихся. Без этого
    Risk-рой никогда не набрал бы 0.7, и его абсолютное вето осталось бы
    недостижимой строкой спеки (проверено на реальном _consensus).
    """
    tally: dict[str, int] = {"BUY": 0, "SELL": 0, "HOLD": 0}
    for v in votes:
        tally[v.action] = tally.get(v.action, 0) + 1
    held = [v for v in votes if v.action == "HOLD"]
    directed = sum(n for a, n in tally.items() if a != "HOLD")
    if held and len(held) > directed:
        denom = sum(float(v.weight) for v in held)
        conf = (sum(float(v.confidence) * float(v.weight) for v in held) / denom
                if denom else 0.0)
        return SwarmVerdict(name, "HOLD", 0.0, round(conf, 3), votes)
    score = Arbiter.score_votes(votes)
    sides = sorted((n for a, n in tally.items() if a != "HOLD" and n),
                   reverse=True)
    deadlock = (len(sides) > 1 and sides[0] == sides[1]) or \
        abs(score) < DEADLOCK_SCORE
    if deadlock or not votes:
        return SwarmVerdict(name, "HOLD", 0.0, 0.0, votes)
    action = "BUY" if score > 0 else "SELL"
    return SwarmVerdict(name, action, score, abs(score), votes)


def mini_swarm_debate(name: str, agents: list, snap: dict,
                      model_of: dict | None = None,
                      *, workers: int = 4):
    """Два раунда дебатов одного мини-роя → (verdict, строки дерева).

    Строки собираются в список, а не печатаются напрямую: три роя
    идут параллельно, и их блоки нельзя перемешивать в выводе.
    """
    model_of = model_of or {}
    lines: list[str] = [f"[swarm] 🏛️ {name} Swarm (Round 1):"]
    # раунд 1 — независимые голоса, параллельно по 4 агентам
    round1: list = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(ag.decide, snap): ag for ag in agents}
        for f in as_completed(futs):
            ag = futs[f]
            try:
                round1.append(f.result())
            except Exception as e:  # noqa: BLE001
                round1.append(SwarmVote(
                    action="HOLD", confidence=0.0,
                    reason=f"[error] decide: {str(e)[:80]}",
                    stop_loss=0.0, agent=ag.name,
                    persona=ag.persona, mode="error",
                    weight=getattr(ag, "weight", 1.0)))
    order = {ag.name: i for i, ag in enumerate(agents)}
    round1.sort(key=lambda v: order.get(v.agent, 99))
    for v in round1:
        lines.append(f"   {v.persona} "
                     f"({model_of.get(v.agent, 'stub')}): "
                     f"{v.action} {v.confidence:.2f} "
                     f"({v.reason[:60]})")
    # Smart Debates: все 4 сошлись — спорить не о чем, раунд 2
    # пропускается (экономия 4 LLM-запросов и секунд цикла).
    if len({v.action for v in round1}) == 1:
        verdict = _consensus(name, round1)
        lines.append(f"[swarm] 🏆 {name} Swarm Consensus: "
                     f"{verdict.action} {verdict.confidence:.2f} "
                     f"(единогласно — дебаты не нужны)")
        return verdict, lines, round1, round1
    # раунд 2 — критика и синтез: голоса коллег в промпте
    round2: list = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_debate_round2, ag, snap, round1): ag
                for ag in agents}
        for f in as_completed(futs):
            ag = futs[f]
            try:
                round2.append(f.result())
            except Exception as e:  # noqa: BLE001
                round2.append(SwarmVote(
                    action="HOLD", confidence=0.0,
                    reason=f"[error] debate: {str(e)[:80]}",
                    stop_loss=0.0, agent=ag.name,
                    persona=ag.persona, mode="error",
                    weight=getattr(ag, "weight", 1.0)))
    round2.sort(key=lambda v: order.get(v.agent, 99))
    lines.append(f"[swarm] ⚔️ {name} Swarm (Round 2 - Debates):")
    for v in round2:
        own = next((o for o in round1 if o.agent == v.agent), v)
        moved = own.action != v.action
        # Строка читается как переход: прежний голос → новый, и в скобках
        # дельта уверенности. Без старого голоса «смена голоса» выглядела
        # бы как обычная строка раунда 2 и проверялась только по метке.
        head = (f"{own.action} → {v.action}" if moved else v.action)
        conf = (f"({own.confidence:.2f} → {v.confidence:.2f})"
                if abs(v.confidence - own.confidence) >= 0.005
                else f"{v.confidence:.2f}")
        mark = " ← сменил голос" if moved else ""
        lines.append(f"   {v.persona}: {head} {conf}{mark} "
                     f"({v.reason[:60]})")
    verdict = _consensus(name, round2)
    # Раунд 1 остаётся при вердикте: история записывает переходы голосов,
    # и окно «Решение» показывает их как данные, а не только как текст.
    verdict.opening = round1
    dead = "" if verdict.confidence else " (deadlock — голос веса 0)"
    lines.append(f"[swarm] 🏆 {name} Swarm Consensus: "
                 f"{verdict.action} {verdict.confidence:.2f}{dead}")
    return verdict, lines, round1, round2


def run_parliament(pool, snap: dict, chaos: bool = False,
                   *, workers: int = 4, on_block=None):
    """Парламент целиком: ростер → модели → дебаты параллельно.

    chaos=True — дебатирует один отряд шторма (Chaos Scalper +
    Volatility Breakout), мета-слой пропускается: вызывающий берёт
    голоса round2 напрямую — у них настоящие CHAOS_PERSONAS, и
    Арбитр примет их как сигнал отряда.

    on_block(name, verdict, lines) — если передан, вызывается для
    каждого роя сразу, как его блок готов в каноническом порядке
    MINI_SWARMS: ответы агентов попадают в лог по мере завершения
    дебатов, а не одной пачкой после самого медленного роя.

    Возвращает (verdicts, lines, round2_votes, model_of): строки
    печатаются в порядке MINI_SWARMS — блоки роев не перемешаны.
    """
    roster = pick_roster(pool, chaos=chaos)
    assign_models(pool, roster)
    model_of = {ag.name: _model_label(getattr(ag, "model_id", ""))
                for crew in roster.values() for ag in crew}
    order = ("Chaos",) if chaos else tuple(MINI_SWARMS)
    results: dict[str, tuple] = {}
    emitted = 0                    # сколько блоков order уже отдано в on_block
    submitted: set[str] = set()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(mini_swarm_debate, name, crew, snap,
                          model_of, workers=workers): name
                for name, crew in roster.items() if crew}
        submitted = set(futs.values())
        for f in as_completed(futs):
            results[futs[f]] = f.result()
            # Flush готового префикса order: блок Technical появляется
            # сразу, даже если Risk ещё думает; рой без фьючерса (пустой
            # состав) пропускаем, чтобы не встать в ожидание навсегда.
            while emitted < len(order) and (
                    order[emitted] not in submitted
                    or order[emitted] in results):
                name = order[emitted]
                emitted += 1
                got = results.get(name)
                if got is not None and on_block is not None:
                    on_block(name, got[0], got[1])
    verdicts: list[SwarmVerdict] = []
    lines: list[str] = []
    round2: list = []
    for name in order:
        got = results.get(name)
        if not got:
            continue
        verdict, block, _r1, r2 = got
        lines += block
        verdicts.append(verdict)
        round2 += r2
    return verdicts, lines, round2, model_of


def meta_votes(verdicts: list) -> list:
    """3 вердикта мини-роев → мета-голоса для arb.rule."""
    return [v.to_vote() for v in verdicts]


def risk_veto(verdicts: list) -> str:
    """Абсолютное вето: Risk Swarm HOLD с confidence > 0.7."""
    for v in verdicts:
        if (v.name == "Risk" and v.action == "HOLD"
                and v.confidence > RISK_VETO_CONF):
            return ("META-VERDICT: HOLD (Risk Swarm Veto — "
                    f"confidence {v.confidence:.2f} > "
                    f"{RISK_VETO_CONF})")
    return ""
