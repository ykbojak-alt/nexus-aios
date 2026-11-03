"""knowledge.py — статическая база правил трейдинга (knowledge_base.json).

Правила проверены бэктестами и здравым смыслом: они не меняются от
сделки к сделке. Динамические уроки Learning Engine (AI-2) их
дополняют, но не заменяют — поэтому системный промпт агента каждый раз
собирается заново: персона → базовые правила → уроки.

Если файл базы недоступен — блок правил пустой, торговля продолжает
работать (деградация честная, без молчаливой подмены).
"""

from __future__ import annotations

import json
from pathlib import Path

KB_FILENAME = "knowledge_base.json"
KB_PATH = Path(__file__).resolve().parent / KB_FILENAME

RULES_HEADER = "Твои базовые правила:"
LESSONS_HEADER = "Твои прошлые ошибки (уроки):"


def load_rules(path: str | Path | None = None) -> list[str]:
    """Базовые правила из knowledge_base.json (пусто — если файла нет)."""
    p = Path(path) if path else KB_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rules = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(rules, list):
        return []
    return [str(r).strip() for r in rules if str(r).strip()]


def rules_block(rules: list[str] | None = None) -> str:
    """Блок базовых правил для системного промпта."""
    items = load_rules() if rules is None else list(rules)
    if not items:
        return ""
    return "\n\n" + RULES_HEADER + "\n" + "\n".join(f"- {r}" for r in items)


def lessons_block(lessons: str) -> str:
    """Блок динамических уроков — дополняет базовые правила."""
    text = (lessons or "").strip()
    if not text:
        return ""
    return "\n\n" + LESSONS_HEADER + "\n" + text


def context_block(lessons: str = "", rules: list[str] | None = None) -> str:
    """Полный блок контекста агента: базовые правила + уроки."""
    return rules_block(rules) + lessons_block(lessons)


def summary(lessons_count: int = 0, rules: list[str] | None = None) -> str:
    """Строка лога TUI: сколько правил и уроков ушло в контекст агентов."""
    items = load_rules() if rules is None else list(rules)
    return (f"Loaded {len(items)} base rules + {int(lessons_count)} "
            f"learned lessons into agent context")
