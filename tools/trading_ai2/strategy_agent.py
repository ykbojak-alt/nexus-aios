"""strategy_agent.py — LLM-мозг: модель → строгий JSON → Pydantic.

Модель и шлюз выбирает llm_factory.LLMFactory по ACTIVE_LLM_MODEL —
переключается на лету, без правки кода. Любой OpenAI-совместимый
шлюз (DeepSeek, OpenAI, Anthropic, OpenRouter) говорит одним SDK.
Ответ обязан быть {"action","amount_percent","confidence",
"reason","stop_loss"} — мусор/провал валидации → безопасный HOLD
(LLM не может «сломать» сделку).

ВАЖНО: агент не имеет доступа к бирже — он возвращает намерение,
исполнение возможно только через loop_guard.RiskManager.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

try:
    from .swarm.llm_factory import LLMFactory
except ImportError:  # pragma: no cover — запуск без пакета
    sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
    from swarm.llm_factory import LLMFactory

try:
    from .swarm.json_sanitizer import _safe_parse_json, has_action, salvage
except ImportError:  # pragma: no cover — запуск без пакета
    from swarm.json_sanitizer import _safe_parse_json, has_action, salvage

try:
    from tools.research_agent_clients import env
except ImportError:  # pragma: no cover — запуск без пакета
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from research_agent_clients import env

SYSTEM_PROMPT = (
    "Ты — торговый агент крипто-фонда. Получаешь JSON с ценой и "
    "индикаторами (RSI, MACD, EMA9/21, ATR), объёмом (volume, "
    "volume_sma20, volume_ratio), режимом рынка (regime), ДВУМЯ "
    "сентиментами (market_sentiment — глобальный фон рынка, "
    "asset_sentiment — новости именно по твоей монете) и блоком mtf с "
    "данными двух таймфреймов (1m и 15m). "
    "Верни СТРОГО JSON без markdown: "
    '{"action": "BUY"|"SELL"|"HOLD", "amount_percent": float 0-100, '
    '"confidence": float 0-1 — уверенность в этом голосе, '
    '"reason": str до 200 символов, "stop_loss": float — цена стопа, '
    'обязательна для BUY, для SELL/HOLD может быть 0, '
    '"persona": str — имя твоего амплуа из system-сообщения}. '
    "Учитывай объём: цена растёт, а объём падает — ложный пробой "
    "(fakeout); цена падает на повышенном объёме — реальная распродажа. "
    "Два сентимента: если глобальный фон плохой, а по твоей монете "
    "хорошие новости — это возможность; если оба плохие — не входи. "
    "При regime=TRENDING_UP тренд вверх, TRENDING_DOWN — вниз, "
    "RANGING — диапазон не ломай, VOLATILE — паника: лонги запрещены, "
    "только HOLD или шорт с малым плечом. Никакого текста вокруг JSON. "
    "Если данных мало — HOLD."
)


def system_prompt(asset: str = "") -> str:
    """Системный промпт под конкретный актив (SOL/USDT, BTC/USDT, …).

    Промпт без актива остаётся общим — так работал рой до перехода
    на мультивалютный портфель.
    """
    sym = (asset or "").strip().upper()
    if not sym:
        return SYSTEM_PROMPT
    return (SYSTEM_PROMPT + f" Ты торгуешь активом {sym}: все цены, "
            f"индикаторы и новости в снапшоте — по {sym}; про другие "
            f"монеты не говори.")


class TradeDecision(BaseModel):
    """Валидированное решение LLM. Контракт между мозгом и Risk Manager."""
    action: Literal["BUY", "SELL", "HOLD"]
    amount_percent: float = Field(ge=0.0, le=100.0)
    reason: str = Field(max_length=500)
    stop_loss: float = Field(ge=0.0)
    persona: str = ""           # амплуа, которое вернула модель (эхо)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    mode: str = "live"          # live | stub | parse_fallback | sanitized

    @field_validator("stop_loss")
    @classmethod
    def buy_requires_stop(cls, v: float, info):
        if info.data.get("action") == "BUY" and v <= 0:
            raise ValueError("BUY без stop_loss")
        return v


def _hold(reason: str, mode: str) -> TradeDecision:
    return TradeDecision(action="HOLD", amount_percent=0.0,
                         reason=reason[:450], stop_loss=0.0, mode=mode)


def _recover_truncated(text: str, model: str, price: float = 0.0,
                       truncated: bool = True,
                       repaired: dict | None = None) -> TradeDecision | None:
    """Спасти битый JSON полевым разбором (swarm/json_sanitizer).

    repaired — починенный объект из _safe_parse_json; если он не задан,
    разбирается здесь же (починенный JSON + регулярки).

    action обязателен и должен быть валидным; reason допускается
    усечённым; confidence и сумма достаются регулярками. Для BUY стоп
    синтезируется (2% от цены — как в заглушке): без известной цены
    восстановление небезопасно — возвращаем None (честный HOLD).
    """
    fields = salvage(text, repaired)
    if not has_action(fields):
        return None

    def _num(key: str, default: float = 0.0) -> float:
        value = fields.get(key)
        return float(value) if isinstance(value, (int, float)) else default

    reason = str(fields.get("reason") or "")[:430]
    if reason and truncated:
        reason += "… [обрезано провайдером]"
    stop = _num("stop_loss")
    action = str(fields["action"]).upper()
    if action == "BUY" and stop <= 0:
        stop = round(price * 0.98, 2) if price > 0 else 0.0
        if stop <= 0:
            return None                    # BUY без стопа — не спасаем
    conf = min(1.0, max(0.0, _num("confidence")))
    try:
        decision = TradeDecision(
            action=action,
            amount_percent=min(100.0, max(0.0, _num("amount_percent", 1.0))),
            reason=reason or "recovered: без причины",
            stop_loss=stop,
            persona=str(fields.get("persona") or "")[:80],
            confidence=conf,
            mode="sanitized")
    except ValueError:
        return None                    # BUY без stop_loss — не спасаем
    print(f"[parser] Recovered truncated JSON for {model}", flush=True)
    return decision


def parse_decision(raw: str, model: str = "",
                   price: float = 0.0) -> TradeDecision:
    """LLM-ответ → TradeDecision.

    Порядок: строгий JSON → (если JSON не разобран) починка обрезанного
    ответа (_safe_parse_json дописывает закрывающие кавычки и скобки) и
    разбор полей регулярными выражениями (лог `[parser] Recovered
    truncated JSON for {model}`) → HOLD с reason "parse_error". JSON,
    который разобран, но не проходит схему, спасением не подменяется —
    только честный HOLD. Мусорная строка никогда не роняет торговлю, но
    и не проходит как решение. Другие модели при мусоре не зовём:
    чиним ответ или честно держим позицию.
    """
    text = raw.strip()
    # снимаем ```json обёртку, если LLM её всё-таки накинул
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
    try:
        data = json.loads(text)
        broken = False
    except (json.JSONDecodeError, TypeError, ValueError):
        data = None
        broken = True
    if isinstance(data, dict):
        data = dict(data)
        action = data.get("action")
        if isinstance(action, str):
            # модель пишет "buy"/"Sell" — регистр не меняет смысла решения
            data["action"] = action.strip().upper()
        try:
            return TradeDecision(**data)
        except (ValueError, TypeError) as e:
            # JSON разобран, но решение невалидно — семантику валидации
            # не подменяем: спасённая сделка опаснее честного HOLD.
            print(f"[parser] Invalid decision JSON: {e}", flush=True)
            return _hold("parse_error", "parse_fallback")
    # JSON не разобран: чиним обрезанный ответ — закрываем кавычки и
    # скобки, висящий ключ и недописанное число отбрасываем.
    repaired = _safe_parse_json(text)
    recovered = _recover_truncated(text, model, price, truncated=broken,
                                   repaired=repaired)
    if recovered is not None:
        return recovered
    return _hold("parse_error", "parse_fallback")


def _stub_decision(snap: dict) -> TradeDecision:
    """Детерминированные правила без LLM — честный stub для dev/backtest.

    RSI-cross: покупаем СВЕЖИЙ уход в перепроданность (пересечение 40
    сверху вниз), фиксируем при RSI > 60. Объём 1.5% — под 2%-капом
    Risk Manager, стоп 2% ниже цены.
    """
    rsi = snap["rsi"]
    rsi_prev = snap.get("rsi_prev", rsi)
    trend = snap["ema9_above_ema21"]
    if rsi < 40 and rsi_prev >= 40:
        return TradeDecision(
            action="BUY", amount_percent=1.5,
            reason=f"stub: RSI {rsi_prev}→{rsi} ушёл ниже 40 — "
                   f"перепроданность, тренд {'+' if trend else '-'}",
            stop_loss=round(snap["price"] * 0.98, 2), mode="stub")
    if rsi > 60:
        return TradeDecision(
            action="SELL", amount_percent=100.0,
            reason=f"stub: RSI {rsi} > 60 — фиксация",
            stop_loss=0.0, mode="stub")
    return _hold(f"stub: RSI {rsi} (prev {rsi_prev}), "
                 f"тренд {'+' if trend else '-'} — жду", "stub")


class StrategyAgent:
    """Консультант по решениям. НИКОГДА не вызывает биржу.

    Модель и шлюз — из LLMFactory (ACTIVE_LLM_MODEL); маршрут
    перечитывается на каждый запрос, поэтому переключение работает
    на лету, без пересоздания агента.
    """

    def __init__(self, model: str | None = None, role: str = "generator"):
        self.requested_model = model
        self.role = role
        route = LLMFactory.resolve(model)
        self.api_key = route.api_key
        self.model = route.model
        self.base_url = route.base_url
        self._client = None
        self._client_key: tuple[str, str] | None = None

    @property
    def live(self) -> bool:
        return bool(self.api_key)

    def _refresh(self):
        """Маршрут перечитывается каждый раз — модель меняется на лету."""
        route = LLMFactory.resolve(self.requested_model)
        self.api_key = route.api_key
        self.model = route.model
        self.base_url = route.base_url
        return route

    @staticmethod
    def _complete(client, model: str, snap: dict,
                  provider: str = "") -> str:
        # mercury-* (inception, diffusion): с response_format отвечает
        # пустотой/обрывом — без него выдаёт валидный JSON, но длиннее
        kw = {} if provider == "inception" else \
            {"response_format": {"type": "json_object"}}
        r = client.chat.completions.create(
            model=model, temperature=0.1,
            max_tokens=600 if provider == "inception" else 400,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(
                    snap, ensure_ascii=False)},
            ], **kw)
        return r.choices[0].message.content or ""

    def _ask_llm(self, snap: dict) -> str:
        route = self._refresh()
        persona_txt = snap.get("persona")
        messages = [{"role": "system",
                     "content": system_prompt(snap.get("symbol"))}]
        if persona_txt:
            # амплуа — отдельным system-сообщением: сильнее условие,
            # чем строка внутри data-JSON
            messages.append({"role": "system", "content": str(persona_txt)})
        messages.append({"role": "user", "content": json.dumps(
            {k: v for k, v in snap.items() if k != "persona"},
            ensure_ascii=False)})
        return LLMFactory.complete(
            role=self.role, model=self.model, messages=messages,
            max_tokens=250)

    def decide(self, snap: dict) -> TradeDecision:
        if not self.live:
            return _stub_decision(snap)
        try:
            return parse_decision(self._ask_llm(snap), model=self.model,
                                  price=float(snap.get("price") or 0.0))
        except Exception as e:  # noqa: BLE001 — сбой API → HOLD, не падение
            return _hold(f"[error] llm_api_error: {e}",
                         "parse_fallback")
