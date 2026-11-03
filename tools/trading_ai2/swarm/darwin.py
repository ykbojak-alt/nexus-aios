"""darwin.py — дарвиновские веса агентов.

Вес — оперативная оценка агента: ошибся на сделке → вес −0.1,
оказался прав → +0.1. Не путать с reputation_score (epoch_rewarder):
репутация — итог эпохи по всему рынку, вес — реакция на конкретный
исход сделки.

Арбитр считает score = Σ (confidence × weight): голос удачного агента
весит больше, голос неудачного — меньше.

Веса живут в swarm/weights.json — переживают перезапуск процесса, как
и репутация. Каждое изменение веса пишется в swarm/weights.log:
"[learning] Agent TrendFollower-01 weight: 1.00 -> 0.80 (wrong
prediction)" — по этому файлу видно, кто из агентов провалился.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

WEIGHTS_PATH = Path(__file__).resolve().parent / "weights.json"
#: Журнал изменений весов — по нему видно динамику за ночь.
WEIGHTS_LOG = Path(__file__).resolve().parent / "weights.log"

#: Границы веса: слишком лёгкий агент не слышен, слишком тяжёлый —
#: диктат одного голоса.
WEIGHT_MIN = 0.1
WEIGHT_MAX = 3.0
#: Шаг веса за одну сделку: ошибся — минус, угадал — плюс.
WEIGHT_STEP = 0.1


def load_weights(path: Path = WEIGHTS_PATH) -> dict:
    """Веса агентов из файла (пустой dict — файла нет или он битый)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_weights(weights: dict, path: Path = WEIGHTS_PATH) -> None:
    """Сохранить веса (ошибка записи не должна ронять торговлю)."""
    try:
        Path(path).write_text(
            json.dumps(weights, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def update_weight(weight: float, pnl_usd: float) -> float:
    """Новый вес после исхода сделки: убыток → −0.1, прибыль → +0.1.

    Шаг аддитивный и одинаковый в обе стороны: одна ошибка не обнуляет
    агента, а серия ошибок честно уводит его голос из консенсуса.
    """
    if pnl_usd < 0:
        new = weight - WEIGHT_STEP
    elif pnl_usd > 0:
        new = weight + WEIGHT_STEP
    else:
        return weight
    return round(min(WEIGHT_MAX, max(WEIGHT_MIN, new)), 4)


def weight_line(name: str, old: float, new: float, pnl_usd: float) -> str:
    """Строка изменения веса для TUI и журнала.

    «[learning] Agent TrendFollower-01 weight: 1.00 -> 0.90
    (wrong prediction)».
    """
    if pnl_usd > 0:
        verdict = "correct prediction"
    elif pnl_usd < 0:
        verdict = "wrong prediction"
    else:
        verdict = "flat outcome"
    return (f"[learning] Agent {name} weight: {old:.2f} -> {new:.2f} "
            f"({verdict})")


def log_weight_change(name: str, old: float, new: float, pnl_usd: float,
                      path: Path = WEIGHTS_LOG) -> None:
    """Дописать изменение веса в журнал (ошибка записи не роняет цикл)."""
    try:
        with Path(path).open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                     f"{weight_line(name, old, new, pnl_usd)} "
                     f"pnl={pnl_usd:+.2f}\n")
    except OSError:
        pass


def apply_outcome(agents, names, pnl_usd: float, *, log: bool = True) -> dict:
    """Пересчитать веса агентов, голосовавших за исполненную сторону.

    Убыток → вес падает, прибыль → растёт. Возвращает
    {имя: {"old":…, "new":…}}. Изменения пишутся в журнал весов.
    """
    by_name = {a.name: a for a in agents}
    changed: dict = {}
    for name in names or []:
        agent = by_name.get(name)
        if agent is None:
            continue
        old = float(getattr(agent, "weight", 1.0))
        new = update_weight(old, pnl_usd)
        agent.weight = new
        changed[name] = {"old": old, "new": new}
    if changed:
        save_weights({a.name: float(getattr(a, "weight", 1.0))
                      for a in agents})
        if log:
            for name, info in changed.items():
                if abs(info["new"] - info["old"]) < 1e-9:
                    continue
                log_weight_change(name, info["old"], info["new"], pnl_usd)
    return changed


def summarize(changed: dict, limit: int = 3) -> str:
    """Короткая строка для лога: «A 1.00→0.80, B 1.00→0.80»."""
    parts = [f"{name} {info['old']:.2f}→{info['new']:.2f}"
             for name, info in list(changed.items())[:limit]]
    tail = f" … +{len(changed) - limit}" if len(changed) > limit else ""
    return ", ".join(parts) + tail
