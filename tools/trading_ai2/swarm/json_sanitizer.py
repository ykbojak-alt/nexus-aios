"""json_sanitizer.py — починка битого JSON-ответа модели.

Модель регулярно обрывает JSON (кончились токены на reasoning) или
обкладывает его текстом. json.loads() такой ответ не берёт, агент
молчит — и в лог падает llm_parse_error.

Здесь ответ чинится в три приёма:
  1) из текста вырезается целый JSON-объект (баланс скобок);
  2) обрезанный объект закрывается (_safe_parse_json дописывает
     закрывающие кавычки и скобки) — если обрыв случился на середине
     reason, поле вытащится;
  3) если JSON не собрался — поля берутся регулярными выражениями:
     action, confidence, reason (+ amount_percent, stop_loss).

Если не вытащилось ничего — вызывающий отдаёт HOLD с reason
"parse_error" (см. strategy_agent.parse_decision) и НЕ зовёт другие
модели: чиним ответ или честно держим позицию.
"""

from __future__ import annotations

import json
import re

#: Ключи, которые нас интересуют в ответе модели.
FIELDS = ("action", "confidence", "amount_percent", "reason", "stop_loss",
          "persona")

_ACTION_STRICT = re.compile(r'"action"\s*:\s*"(?P<v>BUY|SELL|HOLD)"', re.I)
_ACTION_LOOSE = re.compile(r'\baction\b\s*[:=]\s*["\']?(?P<v>BUY|SELL|HOLD)\b',
                           re.I)

_NUM = r'(?P<v>-?\d+(?:\.\d+)?)'
_CONF_STRICT = re.compile(r'"confidence"\s*:\s*' + _NUM, re.I)
_CONF_LOOSE = re.compile(r'\bconfidence\b\s*[:=]\s*' + _NUM, re.I)
_AMOUNT_STRICT = re.compile(r'"amount_percent"\s*:\s*' + _NUM, re.I)
_AMOUNT_LOOSE = re.compile(r'\bamount_percent\b\s*[:=]\s*' + _NUM, re.I)
_STOP_STRICT = re.compile(r'"stop_loss"\s*:\s*' + _NUM, re.I)
_STOP_LOOSE = re.compile(r'\bstop_loss\b\s*[:=]\s*' + _NUM, re.I)
_REASON_STRICT = re.compile(r'"reason"\s*:\s*"(?P<v>(?:[^"\\]|\\.){0,450})"',
                             re.I)
_REASON_OPEN = re.compile(r'"reason"\s*:\s*"(?P<v>[^"\n]{0,450})', re.I)
_PERSONA_STRICT = re.compile(r'"persona"\s*:\s*"(?P<v>[^"\n]{0,80})"', re.I)

#: Хвост обрезанного JSON: ключ без значения ("reason":) — выбрасываем.
_TRAILING_KEY = re.compile(r',?\s*"[^"]*"\s*:\s*$')
#: Хвост обрезанного JSON: недописанное число (1. / 12) — выбрасываем.
_TRAILING_NUMBER = re.compile(r',?\s*-?\d+(?:\.\d*)?\s*$')


def _match(patterns, text: str):
    """Первое совпадение из списка шаблонов (строгий → свободный)."""
    for pat in patterns:
        m = pat.search(text or "")
        if m:
            return m
    return None


def _num(patterns, text: str) -> float | None:
    m = _match(patterns, text)
    if not m:
        return None
    try:
        return float(m.group("v"))
    except (TypeError, ValueError):
        return None


def _balanced_object(text: str) -> str:
    """Первый целый JSON-объект из текста (по балансу скобок)."""
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def _close_truncated(body: str) -> str:
    """Закрыть обрезанный объект: строку, скобки, массивы.

    Провайдер мог оборвать ответ на середине строки ("...reason": "RSI 22 —
    ), на висящем ключе ("...reason":) или на недописанном числе
    ("...amount_percent": 1.). Первые два лечатся дописыванием кавычки
    и скобок, третье — отбрасыванием хвоста, иначе JSON не соберётся.
    """
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in body:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            stack.pop()
    out = body.rstrip()
    if esc:
        out = out[:-1]              # хвостовой "\" закрывать кавычкой нельзя
    if out.endswith(","):
        out = out[:-1]
    if in_str:
        out += '"'
    out = _TRAILING_KEY.sub("", out)          # ключ без значения — выкинуть
    out += "".join("}" if b == "{" else "]" for b in reversed(stack))
    return out


def _close_candidates(body: str) -> list[str]:
    """Варианты починки обрезанного JSON: от полного к подрезанным хвостам.

    Первый кандидат — самый полный (дописанная строка + скобки). Если он
    не собирается (висящий ключ, недописанное число) — режем хвост и
    пробуем снова.
    """
    out: list[str] = []
    text = body
    for _ in range(4):
        cand = _close_truncated(text)
        if cand and cand not in out:
            out.append(cand)
        trimmed = _TRAILING_NUMBER.sub("", text).rstrip()
        if trimmed == text.rstrip():
            trimmed = _TRAILING_KEY.sub("", text).rstrip()
        if trimmed == text.rstrip():
            break
        text = trimmed
    return out


def repair_json(text: str) -> str:
    """Починить JSON: целый объект либо обрезанный, закрытый скобками.

    Пустая строка — чинить нечего.
    """
    text = text or ""
    whole = _balanced_object(text)
    if whole:
        try:
            json.loads(whole)
            return whole
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    if start < 0:
        return ""
    for cand in _close_candidates(text[start:]):
        try:
            json.loads(cand)
            return cand
        except json.JSONDecodeError:
            continue
    return ""


def _safe_parse_json(text: str) -> dict | None:
    """Разбор JSON-ответа модели, который мог прийти обрезанным.

    Порядок: строгий json.loads → целый объект по балансу скобок →
    починка обрезанного ответа (дописываются закрывающие кавычки и
    скобки, отбрасывается висящий ключ и недописанное число).

    None — разобрать нечего: вызывающий обязан честно ответить HOLD,
    а не звать другие модели.
    """
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw[:4].lower() == "json":
            raw = raw[4:]
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    whole = _balanced_object(raw)
    if whole:
        try:
            data = json.loads(whole)
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    start = raw.find("{")
    if start < 0:
        return None
    for cand in _close_candidates(raw[start:]):
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _reason(text: str) -> str:
    m = _REASON_STRICT.search(text or "")
    if m:
        return m.group("v").replace('\\"', '"').strip()
    m = _REASON_OPEN.search(text or "")   # обрыв на середине reason
    if m:
        return m.group("v").replace('\\"', '"').strip()
    return ""


def extract(text: str) -> dict:
    """Вытащить поля битого ответа регулярными выражениями.

    Возвращает только те поля, которые реально нашлись.
    """
    text = text or ""
    out: dict = {}
    m = _match([_ACTION_STRICT, _ACTION_LOOSE], text)
    if m:
        out["action"] = m.group("v").upper()
    conf = _num([_CONF_STRICT, _CONF_LOOSE], text)
    if conf is not None:
        out["confidence"] = conf
    amount = _num([_AMOUNT_STRICT, _AMOUNT_LOOSE], text)
    if amount is not None:
        out["amount_percent"] = amount
    stop = _num([_STOP_STRICT, _STOP_LOOSE], text)
    if stop is not None:
        out["stop_loss"] = stop
    reason = _reason(text)
    if reason:
        out["reason"] = reason
    m = _PERSONA_STRICT.search(text)
    if m:
        out["persona"] = m.group("v").strip()
    return out


def salvage(text: str, repaired: dict | None = None) -> dict:
    """Восстановленные поля ответа: сначала починенный JSON, потом регулярки.

    repaired — уже разобранный починенный объект (если вызывающий звал
    _safe_parse_json сам); иначе он разбирается здесь.

    Пустой словарь — восстановить нечего (агент должен ответить HOLD
    с reason "parse_error").
    """
    out: dict = {}
    fixed = repaired if repaired is not None else _safe_parse_json(text)
    if fixed:
        for key in FIELDS:
            if key in fixed:
                out[key] = fixed[key]
    for key, value in extract(text).items():
        out.setdefault(key, value)
    return out


def has_action(fields: dict) -> bool:
    """Есть ли в восстановленных полях действие."""
    return str(fields.get("action") or "").upper() in ("BUY", "SELL", "HOLD")
