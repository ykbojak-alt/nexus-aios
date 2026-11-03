"""persona_generator.py — генератор личностей для пула из 100 агентов.

15 базовых амплуа (_PERSONAS) — это «семьи». Пул из 100 агентов не может
дать всем одинаковый промпт: внутри одной семьи нужны разные характеры.
Генератор собирает count уникальных вариантов: имя «Семья-Вариант»
(TrendFollower-Aggressive, Contrarian-RSI, Contrarian-MACD, …), и у
каждого свой системный промпт — амплуа семьи плюс личный характер
(риск, фокус индикаторов, темп).

Инвариант: имена и промпты уникальны — два агента роя не могут звучать
одинаково. Нарушение инварианта — ошибка, а не тихий дефект.
"""

from __future__ import annotations

from dataclasses import dataclass

try:
    from .agent_swarm import _PERSONAS, PERSONA_TIMEFRAME, DEFAULT_TIMEFRAME
except ImportError:  # pragma: no cover — запуск без пакета
    from swarm.agent_swarm import (_PERSONAS, PERSONA_TIMEFRAME,
                                   DEFAULT_TIMEFRAME)

#: Характеры внутри любой семьи. Порядок задаёт разброс: первые — по
#: риску, дальше — по фокусу индикаторов, последние — по темпу.
#: (метка, характер, риск, стоп %, сдвиг порога)
_VARIANTS: tuple[tuple[str, str, str, float, float], ...] = (
    ("Aggressive",
     "Твой характер — агрессивный риск: размер позиции до 2% эквити, "
     "стоп шире (1.5% от цены), confidence не ниже 0.5. Одно сильное "
     "движение лучше десяти слабых, но за широкой стопой.",
     "high", 1.5, 0.10),
    ("Conservative",
     "Твой характер — строгий риск: вход только после подтверждения "
     "(второй сигнал или объём), стоп узкий (0.6% от цены), confidence не "
     "выше 0.5. Лучше пропустить сделку, чем стать её причиной.",
     "low", 0.6, -0.10),
    ("RSI",
     "Твой характер — фокус только на RSI: уровень экстремума и его "
     "динамика. MACD и EMA не смотришь вообще, твой сигнал — RSI сам по "
     "себе и изменение RSI за последний бар.",
     "medium", 1.0, 0.0),
    ("MACD",
     "Твой характер — фокус только на MACD: знак и сила гистограммы. "
     "RSI не смотришь, тренд по EMA для тебя вторичен, решает "
     "гистограмма и её изменение.",
     "medium", 1.0, 0.0),
    ("EMA",
     "Твой характер — фокус только на EMA: положение цены относительно "
     "EMA9/21 и их наклон. RSI и MACD не смотришь — твой сигнал говорит "
     "только структура средних.",
     "medium", 1.0, 0.0),
    ("Volume",
     "Твой характер — фокус только на объёме: нет объёма — нет сделки. "
     "Каждый вход подтверждай объёмом, любой сигнал без объёма — это "
     "ловушка, а не сигнал.",
     "medium", 1.0, 0.0),
    ("Volatility",
     "Твой характер — фокус только на волатильности: ATR решает, входить "
     "ли вообще. В тихую погоду торгуешь смело, в шторм (ATR выросла "
     "больше чем на 50% за час) — не входишь вовсе.",
     "medium", 1.0, 0.0),
    ("Breakout",
     "Твой характер — фокус на пробоях: вход только на выходе из "
     "диапазона, внутри диапазона — HOLD. Ложный пробой без объёма не "
     "берёшь.",
     "medium", 1.0, 0.0),
    ("MeanReversion",
     "Твой характер — фокус на возврате к среднему: вход на отклонении "
     "цены от среднего, а не на её продолжении. Если отклонение "
     "сопровождается паническим объёмом — не лови падение.",
     "medium", 1.0, 0.0),
    ("Fast",
     "Твой характер — быстрый: короткое удержание, вход по первому "
     "подтверждению, стоп 0.8% от цены. Ты уходишь из позиции, когда "
     "сделка не пошла, даже если сигнал был сильным.",
     "medium", 0.8, 0.05),
    ("Slow",
     "Твой характер — медленный: длинное удержание, вход только после "
     "полного подтверждения, стоп 1.2% от цены. Ты ждёшь, пока тренд "
     "покажет себя целиком, и не торгуешь первой свечой.",
     "medium", 1.2, -0.05),
    ("Balanced",
     "Твой характер — баланс: без резких акцентов, решение по совокупности "
     "сигналов, стоп 1.0% от цены. Ты не ищешь идеальный вход — ты "
     "ищешь честный.",
     "medium", 1.0, 0.0),
)


@dataclass(frozen=True)
class PersonaVariant:
    """Один агент пула: личность внутри семьи.

    name — уникальное имя агента («TrendFollower-Aggressive»);
    family — базовая амплуа (логика stub-голосования);
    variant — характер («Aggressive»);
    prompt — уникальный системный промпт;
    timeframe — приоритетный таймфрейм семьи;
    risk / stop_pct / bias — параметры характера.
    """
    name: str
    family: str
    variant: str
    prompt: str
    timeframe: str = DEFAULT_TIMEFRAME
    risk: str = "medium"
    stop_pct: float = 1.0
    bias: float = 0.0


def _families() -> list[tuple[str, str]]:
    """Семьи и их базовые промпты: [(семья, промпт), …]."""
    return [(name, prompt) for name, _count, prompt in _PERSONAS]


def _plan(count: int, n_fams: int) -> list[tuple[int, int]]:
    """Раскладка агентов по семьям: [(семья, номер характера), …].

    Семьи идут подряд, поэтому список читается как «семья → её
    характеры»; остаток от деления достаётся первым семьям.
    """
    base, extra = divmod(count, n_fams)
    plan: list[tuple[int, int]] = []
    for fi in range(n_fams):
        for slot in range(base + (1 if fi < extra else 0)):
            plan.append((fi, slot))
    return plan


def generate_personas(count: int = 100) -> list[PersonaVariant]:
    """count уникальных персон: семьи × характер, имя «Семья-Вариант».

    Раскладка по кругу: каждая семья получает столько вариантов, сколько
    нужно, и ни один вариант внутри семьи не повторяется. При count=100
    на 13 семей приходится 7–8 агентов на семью, имена и промпты
    уникальны.
    """
    count = int(count)
    if count <= 0:
        return []
    fams = _families()
    if not fams:
        return []
    variants: list[PersonaVariant] = []
    for family_index, slot in _plan(count, len(fams)):
        family, base_prompt = fams[family_index]
        cycle, vidx = divmod(slot, len(_VARIANTS))
        label, clause, risk, stop_pct, bias = _VARIANTS[vidx]
        if cycle:
            # больше агентов, чем характеров: второй круг получает
            # нумерованный характер — имя и промпт остаются уникальными
            label = f"{label}-{cycle + 1}"
            clause = (f"{clause} Это номер {cycle + 1} в своей семье — "
                      f"отличайся от однофамильцев сосредоточенностью.")
        name = f"{family}-{label}"
        prompt = (f"{base_prompt} {clause}")
        variants.append(PersonaVariant(
            name=name, family=family, variant=label, prompt=prompt,
            timeframe=PERSONA_TIMEFRAME.get(family, DEFAULT_TIMEFRAME),
            risk=risk, stop_pct=stop_pct, bias=bias))
    _assert_unique(variants)
    return variants


def _assert_unique(variants: list[PersonaVariant]) -> None:
    """Инвариант уникальности: нет двух одинаковых имён или промптов."""
    names = [v.name for v in variants]
    prompts = [v.prompt for v in variants]
    if len(set(names)) != len(names):
        raise ValueError("имена персон не уникальны")
    if len(set(prompts)) != len(prompts):
        raise ValueError("промпты персон не уникальны")


def persona_summary(variants: list[PersonaVariant], examples: int = 10) -> str:
    """Строка лога TUI: сколько персон, сколько семей, пример имён."""
    fams = len({v.family for v in variants})
    names = ", ".join(v.name for v in variants[:examples])
    return (f"[personas] {len(variants)} уникальных персон · {fams} семей · "
            f"пример: {names}")


def find_variant(variants: list[PersonaVariant],
                 name: str) -> PersonaVariant | None:
    """Персона по имени (для точечной замены агента в пуле)."""
    for v in variants:
        if v.name == name:
            return v
    return None


def variant_catalog() -> list[PersonaVariant]:
    """Полный каталог личностей: все пары «семья × характер»."""
    fams = _families()
    return generate_personas(len(fams) * len(_VARIANTS))


def clone_variant(variant: PersonaVariant, cycle: int) -> PersonaVariant:
    """Клон личности с номером — когда уникальные варианты кончились.

    Имя и промпт остаются уникальными: клон отличается номером и
    требованием «отличись от однофамильцев».
    """
    return PersonaVariant(
        name=f"{variant.name}-{cycle}",
        family=variant.family,
        variant=f"{variant.variant}-{cycle}",
        prompt=(f"{variant.prompt} Это клон характера «{variant.variant}» "
                f"под номером {cycle} — отличи его настройкой порогов."),
        timeframe=variant.timeframe, risk=variant.risk,
        stop_pct=variant.stop_pct, bias=variant.bias)
