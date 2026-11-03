"""agent_swarm.py — пул из 60 агентов-стратегов.

Каждый агент — персона (system prompt) + параметры. С DEEPSEEK_API_KEY
агент спрашивает LLM со своим промптом; без ключа — детерминированный
stub по персоне (mode помечен, не маскируется под LLM). Голос:
{action, confidence 0-1, reason, stop_loss} — жёстко через Pydantic.

Аналитика многомерная: снапшот несёт два таймфрейма (1m и 15m), объём
с его средней и режим рынка. У каждой персоны свой приоритетный
таймфрейм (PERSONA_TIMEFRAME) — Scalper и Momentum смотрят на 1m,
TrendFollower, Breakout и MeanReversion — на 15m.

Рой работает с динамическим активом: make_swarm(symbol="SOL/USDT")
собирает промпты под этот актив, а не под BTC.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from pydantic import BaseModel, Field
from typing import Literal

try:
    from .knowledge import lessons_block, rules_block
except ImportError:  # pragma: no cover — запуск без пакета
    from swarm.knowledge import lessons_block, rules_block

#: Приоритетный таймфрейм персоны: 1m — краткосрочный, 15m — среднесрочный.
PERSONA_TIMEFRAME = {
    "Scalper": "1m",
    "Momentum": "1m",
    "TrendFollower": "15m",
    "Breakout": "15m",
    "MeanReversion": "15m",
}
DEFAULT_TIMEFRAME = "1m"

#: Правило объёма — одинаковое для всех персон.
VOLUME_RULE = (" Обязательно учитывай объём: цена растёт, а объём падает — "
               "ложный пробой (fakeout); цена падает на повышенном объёме — "
               "реальная распродажа (panic selling). Пробой без объёма — "
               "ловушка.")

#: Что означают режимы рынка из снапшота.
REGIME_RULE = (" Учитывай режим рынка (regime) из снапшота: TRENDING_UP — "
               "тренд вверх, TRENDING_DOWN — тренд вниз, RANGING — флэт "
               "(диапазон не ломай), VOLATILE — паника.")

#: Паника: жёсткий запрет лонгов.
PANIC_RULE = (" Рынок в панике. Запрещено открывать лонги. Только HOLD или "
              "шорт с малым плечом.")

#: News Engine v2.0: два сентимента — глобальный фон и по-активный.
SENTIMENT_RULE = (" В снапшете два сентимента: market_sentiment (глобальный "
                  "фон) и asset_sentiment (новости именно по активу). Учитывай "
                  "оба: глобальный фон плохой, а по активу новости хорошие — "
                  "это возможность; оба плохие — не входи.")


def asset_rule(asset: str = "") -> str:
    """Инструкция персоне: по какому активу принимается решение.

    Промпт строится под тот актив, который передан (SOL/USDT, BTC/USDT,
    …) — рой больше не заточено под BTC.
    """
    sym = (asset or "").strip().upper()
    if not sym:
        return ""
    return (f" Ты торгуешь активом {sym}. Все цены, индикаторы и новости в "
            f"снапшоте — по {sym}; про другие монеты не говори.")


def sentiment_rule(asset: str = "") -> str:
    """Правило двух сентиментов под конкретный актив."""
    sym = (asset or "").strip().upper()
    if not sym:
        return SENTIMENT_RULE
    return (f" У тебя два сентимента: market_sentiment (глобальный фон "
            f"рынка) и asset_sentiment (новости именно по {sym}). Учитывай "
            f"оба: глобальный фон плохой, а по {sym} новости хорошие — "
            f"это возможность; оба плохие — не входи.")


def _sent(value) -> float | None:
    """Сентимент из снапшота (None — данных нет, а не ноль)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_sent(value: float | None) -> str:
    """Сентимент в строку лога: «+0.31» или «?», если данных нет."""
    return "?" if value is None else f"{value:+.2f}"


def timeframe_rule(tf: str) -> str:
    """Инструкция персоне: на каком таймфрейме принимать решение."""
    return (f" Твой приоритетный таймфрейм: {tf}. В снапшоте данные по 1m "
            f"и 15m (блок mtf) — решение принимай по своему таймфрейму, "
            f"второй используй как фильтр.")


def regime_block(regime: str = "") -> str:
    """Блок режима рынка для промпта: при панике — запрет лонгов."""
    if (regime or "").upper() == "VOLATILE":
        return "\n\n" + PANIC_RULE
    return ""


class SwarmVote(BaseModel):
    action: Literal["BUY", "SELL", "HOLD"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(max_length=300)
    stop_loss: float = Field(ge=0.0)
    agent: str = ""
    persona: str = ""           # амплуа голосовавшего агента
    mode: str = "stub"          # stub | live | parse_fallback | sanitized
    weight: float = Field(default=1.0, ge=0.0)   # дарвиновский вес агента
    error_status: int = 0         # HTTP-статус отказа LLM (0 — не было)


@dataclass
class SwarmAgent:
    """Один голос роя. reputation_score правит весом в консенсусе
    и обновляется epoch_rewarder по итогам эпохи. weight — оперативный
    дарвиновский вес: падает на убытке, растёт на прибыли (darwin.py)."""
    name: str
    persona: str                # амплуа агента (см. _PERSONAS)
    system_prompt: str
    reputation_score: float = 1.0
    bias: float = 0.0           # сдвиг порогов персоны (разброс роя)
    llm: object | None = None   # StrategyAgent, если есть ключ
    pnl_usdt: float = 0.0       # атрибутированный PnL за эпоху
    base_prompt: str = ""       # текст персоны без уроков (уроки дописываются сверху)
    model_id: str = ""          # OpenRouter id модели агента (SWARM_GEN_MODELS)
    knowledge_block: str = ""   # базовые правила (knowledge_base.json)
    weight: float = 1.0         # дарвиновский вес (score = Σ conf × weight)
    timeframe: str = DEFAULT_TIMEFRAME   # приоритетный таймфрейм персоны
    asset: str = ""             # актив, под который собраны промпты
    persona_prompt: str = ""    # текст амплуа без правил (правила собираются)
    variant: str = ""           # характер внутри семьи («Aggressive»)

    def compose_base(self) -> str:
        """Базовый промпт персоны под текущий актив.

        Промпт пересобирается целиком: амплуа → актив → таймфрейм →
        объём → режим → два сентимента. Смена актива (скринер может
        выбрать SOL вместо BTC) пересобирает промпты, а не меняет
        агента на другого.
        """
        return ((self.persona_prompt or self.base_prompt or "")
                + asset_rule(self.asset) + timeframe_rule(self.timeframe)
                + VOLUME_RULE + REGIME_RULE + sentiment_rule(self.asset))

    def refresh_prompt(self, lessons: str = "", regime: str = "",
                       asset: str = "") -> None:
        """Пересобрать системный промпт: персона → базовые правила → уроки.

        База знаний статична и пересобирается при каждом обновлении —
        динамические уроки дополняют правила, а не затирают их. Режим
        рынка доливается в конец: при VOLATILE — жёсткий запрет лонгов.
        asset — новый актив цикла: промпт собирается под него.
        """
        if asset:
            self.asset = asset.strip().upper()
        self.base_prompt = self.compose_base()
        self.system_prompt = (self.base_prompt + self.knowledge_block
                              + lessons_block(lessons) + regime_block(regime))

    def decide(self, snap: dict) -> SwarmVote:
        if self.llm and self.llm.live:
            return self._llm_vote(snap)
        return self._stub_vote(snap)

    # ----------------------------------------------------------- LLM
    def _llm_vote(self, snap: dict) -> SwarmVote:
        import json
        raw = ""
        try:
            # приоритетный таймфрейм — прямо в запросе, чтобы модель
            # смотрела свой блок mtf, а не путала 1m и 15m
            d = self.llm.decide({**snap, "persona": self.system_prompt,
                                 "priority_timeframe": self.timeframe})
            # confidence модели, если она его назвала; иначе — по размеру
            conf = d.confidence or min(0.9, d.amount_percent / 20 + 0.3)
            return SwarmVote(action=d.action,
                             confidence=min(0.9, max(0.0, conf)),
                             reason=d.reason[:300],
                             stop_loss=d.stop_loss,
                             agent=self.name, persona=self.persona,
                             mode=d.mode, weight=self.weight)
        except Exception as e:  # noqa: BLE001
            status = 0
            try:
                from tools.trading_ai2.swarm.llm_factory import LLMFactory
                status = LLMFactory._status_of(e) or 0
            except Exception:  # noqa: BLE001 — статус не критичен
                pass
            return SwarmVote(action="HOLD", confidence=0.0,
                             reason=f"[error] llm_error: {str(e)[:100]}",
                             stop_loss=0.0, agent=self.name,
                             persona=self.persona,
                             mode="parse_fallback", weight=self.weight,
                             error_status=status)

    # ----------------------------------------------------------- stub
    def _stub_vote(self, snap: dict) -> SwarmVote:
        """Детерминированные правила персоны — разнообразие голосов
        без LLM: каждая амплуа смотрит на свои индикаторы.

        Персона читает свой таймфрейм: Scalper и Momentum — 1m,
        TrendFollower, Breakout и MeanReversion — 15m (блок mtf).
        Объём учитывается: пробой без объёма — ловушка.
        """
        tf = getattr(self, "timeframe", DEFAULT_TIMEFRAME) or DEFAULT_TIMEFRAME
        frame = (snap.get("mtf") or {}).get(tf) or snap
        rsi = float(frame.get("rsi", snap.get("rsi", 50.0)))
        trend = bool(frame.get("ema9_above_ema21",
                               snap.get("ema9_above_ema21", False)))
        macd = float(frame.get("macd", snap.get("macd", 0.0)))
        price = float(frame.get("price", snap.get("price", 0.0)))
        rsi_prev = float(frame.get("rsi_prev", rsi))
        atr_g = float(frame.get("atr_growth_pct",
                                snap.get("atr_growth_pct", 0.0)))
        vol_ratio = float(frame.get("volume_ratio",
                                    snap.get("volume_ratio", 1.0)))
        # уровень 2 важнее уровня 1: новости именно по активу точнее фона
        mkt = _sent(snap.get("market_sentiment", snap.get("sentiment_score")))
        asset_sent = _sent(snap.get("asset_sentiment"))
        ema21 = float(frame.get("ema21", price))
        dist = (price - ema21) / ema21 * 100 if ema21 else 0.0
        p = self.persona
        v = lambda a, c, r, sl=0.0: SwarmVote(
            action=a, confidence=round(max(0.0, min(1.0, c)), 3),
            reason=r, stop_loss=sl, agent=self.name,
            persona=self.persona, mode="stub", weight=self.weight)

        if p == "TrendFollower":
            if trend and macd > 0:
                return v("BUY", 0.55 + self.bias,
                         f"trend: EMA9>21, MACD {macd:+.0f}, объём ×{vol_ratio:.2f}",
                         round(price * 0.98, 2))
            if not trend and macd < 0:
                return v("SELL", 0.55,
                         f"trend: EMA9<21, MACD {macd:+.0f}, объём ×{vol_ratio:.2f}")
            return v("HOLD", 0.3, "trend: направление размыто")
        if p == "Contrarian":
            if rsi < 30 + self.bias * 10:
                return v("BUY", 0.5 + (30 - rsi) / 100,
                         f"contrarian: RSI {rsi} — перепродан, покупаю страх",
                         round(price * 0.975, 2))
            if rsi > 70:
                return v("SELL", 0.6, f"contrarian: RSI {rsi} — жадность")
            return v("HOLD", 0.3, f"contrarian: RSI {rsi} — нет крайности")
        if p == "RiskManager":
            if atr_g > 50:
                return v("HOLD", 0.7, f"risk: ATR выросла на {atr_g:.0f}%/час — вне рынка")
            if not trend:
                return v("HOLD", 0.5, "risk: тренд неясен — капитал важнее")
            return v("HOLD", 0.4, "risk: сигнал слабый — пропускаю")
        if p == "Scalper":
            if macd > 10:
                return v("BUY", 0.45,
                         f"scalp: микро-импульс MACD {macd:+.0f}, объём ×{vol_ratio:.2f}",
                         round(price * 0.995, 2))
            if macd < -10:
                return v("SELL", 0.45,
                         f"scalp: микро-откат MACD {macd:+.0f}, объём ×{vol_ratio:.2f}")
            return v("HOLD", 0.25, "scalp: рынок плоский")
        if p == "Analyst":
            if mkt is None and asset_sent is None:
                return v("HOLD", 0.3, "analyst: нет новостного фона")
            ast = asset_sent
            note = ""
            if ast is None:
                # новостей по активу нет — работаем по глобальному фону
                ast = mkt
                note = " (по активу новостей нет — беру глобальный фон)"
            if mkt is not None and mkt < -0.2 and ast < -0.2:
                return v("HOLD", 0.65,
                         f"analyst: оба сентимента плохие "
                         f"(фон {_fmt_sent(mkt)}, актив {_fmt_sent(ast)}) — "
                         f"не вхожу")
            if mkt is not None and mkt < -0.2 and ast > 0.2:
                return v("BUY", min(0.85, 0.4 + ast * 0.4),
                         f"analyst: фон плохой {_fmt_sent(mkt)}, но по активу "
                         f"{_fmt_sent(ast)} — возможность{note}",
                         round(price * 0.97, 2))
            if ast > 0.2:
                return v("BUY", min(0.85, 0.4 + ast * 0.4),
                         f"analyst: по активу {_fmt_sent(ast)}, фон "
                         f"{_fmt_sent(mkt)} — позитив{note}",
                         round(price * 0.97, 2))
            if ast < -0.2:
                return v("SELL", min(0.85, 0.4 + abs(ast) * 0.4),
                         f"analyst: по активу {_fmt_sent(ast)}, фон "
                         f"{_fmt_sent(mkt)} — негатив{note}")
            return v("HOLD", 0.35,
                     f"analyst: сентименты нейтральны "
                     f"(фон {_fmt_sent(mkt)}, актив {_fmt_sent(ast)})")
        if p == "Breakout":
            if dist > 1.5:
                if vol_ratio < 1.0:
                    return v("HOLD", 0.35,
                             f"breakout: {dist:+.1f}% над EMA21, но объём ×{vol_ratio:.2f} "
                             "— пробой без объёма, ловушка (fakeout)")
                return v("BUY", min(0.85, 0.4 + dist / 10),
                         f"breakout: {dist:+.1f}% над EMA21 — пробой, объём ×{vol_ratio:.2f}",
                         round(price * 0.98, 2))
            if dist < -1.5:
                if vol_ratio > 1.5:
                    return v("SELL", min(0.85, 0.5 + abs(dist) / 10),
                             f"breakout: {dist:+.1f}% под EMA21 на объёме ×{vol_ratio:.2f} "
                             "— реальная распродажа (panic selling)")
                return v("SELL", min(0.85, 0.4 + abs(dist) / 10),
                         f"breakout: {dist:+.1f}% под EMA21 — пробой вниз")
            return v("HOLD", 0.3, f"breakout: {dist:+.1f}% — внутри диапазона")
        if p == "MeanReversion":
            if dist < -2 - self.bias * 2:
                return v("BUY", 0.65, f"mr: {dist:+.1f}% под EMA21 — отскок",
                         round(price * 0.98, 2))
            if dist > 2:
                return v("SELL", 0.6, f"mr: {dist:+.1f}% над EMA21 — перегрев")
            return v("HOLD", 0.35, f"mr: отклонение {dist:+.1f}%")
        if p == "MacdTrader":
            if macd > 50:
                return v("BUY", min(0.9, 0.4 + macd / 1000),
                         f"macd: {macd:+.0f} > +50", round(price * 0.98, 2))
            if macd < -50:
                return v("SELL", min(0.85, 0.4 + abs(macd) / 1000),
                         f"macd: {macd:+.0f} < -50")
            return v("HOLD", 0.3, f"macd: {macd:+.0f} — нулевая зона")
        if p == "Momentum":
            if macd > 100 and trend:
                if vol_ratio < 1.0:
                    return v("HOLD", 0.35,
                             f"momentum: импульс MACD {macd:+.0f}, но объём ×{vol_ratio:.2f} "
                             "— импульс без объёма, ловушка")
                return v("BUY", min(0.9, 0.5 + macd / 1000),
                         f"momentum: импульс MACD {macd:+.0f} по тренду, "
                         f"объём ×{vol_ratio:.2f}",
                         round(price * 0.98, 2))
            if macd < -100 and not trend:
                if vol_ratio > 1.5:
                    return v("SELL", min(0.85, 0.55 + abs(macd) / 1000),
                             f"momentum: откат MACD {macd:+.0f} на объёме ×{vol_ratio:.2f} "
                             "— паническая распродажа")
                return v("SELL", min(0.85, 0.5 + abs(macd) / 1000),
                         f"momentum: импульс MACD {macd:+.0f} против")
            return v("HOLD", 0.3, f"momentum: MACD {macd:+.0f} — импульс слаб")
        if p == "RsiWatcher":
            if rsi < 35 and rsi < rsi_prev - 3:
                return v("BUY", min(0.9, (35 - rsi + 10) / 30),
                         f"rsi-watch: {rsi_prev}→{rsi} — свежая перепроданность",
                         round(price * 0.98, 2))
            if rsi > 70:
                return v("SELL", 0.75, f"rsi-watch: {rsi} > 70")
            return v("HOLD", 0.3, f"rsi-watch: {rsi} — динамики нет")
        if p == "Volatility":
            if atr_g > 50:
                return v("HOLD", 0.65, f"vol: ATR +{atr_g:.0f}%/час — не вхожу")
            if trend:
                return v("BUY", 0.5 + self.bias,
                         f"vol: ATR спокойна ({atr_g:+.0f}%), тренд вверх",
                         round(price * 0.985, 2))
            return v("HOLD", 0.35, "vol: тренда нет")
        if p == "StopHunter":
            if rsi > 75:
                return v("SELL", 0.7, f"stop-hunt: RSI {rsi} — собираю стопы лонгов")
            if rsi < 25:
                return v("BUY", 0.7, f"stop-hunt: RSI {rsi} — стопы шортов снесены",
                         round(price * 0.97, 2))
            return v("HOLD", 0.3, f"stop-hunt: RSI {rsi} — скоплений нет")
        return v("HOLD", 0.6, "hodler: не торгую — храню")


#: Общая инструкция Memory — входит в промпт каждой персоны.
_MEM_RULE = (" Обязательно проверь свои прошлые уроки: если урок "
             "противоречит твоему сигналу — снизь confidence.")

#: 12 агентов — у каждого своё амплуа (вес 1: при n≥11 — по одному
#: на персону, лишние слоты уходят первым по списку). Каждый промпт
#: зеркалит stub-правила персоны — LLM и детерминированный фолбэк
#: голосуют одинаковой логикой.
_PERSONAS = [
    ("TrendFollower", 1,
     "Ты — Trend Follower. Торгуешь только согласованный тренд на 15m: "
     "EMA9>EMA21 и MACD>0 — BUY; EMA9<EMA21 и MACD<0 — SELL. "
     "Рассинхронизация (EMA и MACD смотрят в разные стороны) — HOLD, "
     "направление размыто, не гадай. RSI игнорируешь — он обманчив. "
     "Твоя уязвимость — пилы во флэте: в RANGING снижай confidence."),
    ("Contrarian", 1,
     "Ты — Contrarian. Покупаешь страх, продаёшь жадность: RSI < 30 — "
     "перепроданность, BUY, и чем ниже RSI, тем выше confidence; "
     "RSI > 70 — перекупленность, SELL. Между 30–70 — HOLD, крайности "
     "нет. Тренд и MACD игнорируешь. Твой риск — ловля ножей: крайность "
     "может углубляться, поэтому никогда не покупай RSI < 30 с "
     "confidence выше 0.8."),
    ("RiskManager", 1,
     "Ты — Risk Manager. Ты не ищешь сделки — ты ищешь, почему её "
     "нельзя делать. Голосуешь HOLD всегда, но твоя ценность — точная "
     "причина в reason: ATR выросла > 50%/час — «вне рынка»; тренд "
     "неясен — «капитал важнее»; сигнал слабый — «пропускаю»; "
     "сентименты оба негативные — «новостной шторм». Чем конкретнее "
     "причина, тем полезнее твой голос Арбитру."),
    ("Scalper", 1,
     "Ты — Scalper на 1m. Торгуешь микро-импульсы: MACD > +10 — BUY, "
     "MACD < -10 — SELL, |MACD| < 10 — рынок плоский, HOLD. Крупный "
     "тренд не нужен — берёшь короткое движение и выходишь. Стоп "
     "очень короткий: 0.5% от цены. Большое движение против тебя "
     "больше пары процентов — твой сигнал уже мёртв."),
    ("Analyst", 1,
     "Ты — News/Sentiment Analyst. Решаешь только по сентиментам, "
     "индикаторы (RSI, MACD, EMA) полностью игнорируешь. Пороги ±0.2: "
     "оба сентимента ниже -0.2 — HOLD, не входи в шторм. Фон ниже -0.2, "
     "а по активу выше +0.2 — BUY, это расхождение и есть возможность. "
     "По активу выше +0.2 при нейтральном фоне — BUY. По активу ниже "
     "-0.2 — SELL. Нет данных по активу — работай по глобальному фону "
     "и скажи об этом в reason. Нет данных вообще — HOLD."),
    ("Breakout", 1,
     "Ты — Breakout-трейдер на 15m. Меришь отклонение цены от EMA21: "
     "выше на 1.5%+ — пробой вверх, BUY, но только если volume_ratio "
     "≥ 1.0: пробой без объёма — ловушка маркетмейкера, HOLD. Ниже "
     "на 1.5%+ — пробой вниз, SELL; при volume_ratio > 1.5 это уже "
     "panic selling — confidence выше. Внутри ±1.5% — HOLD, диапазон "
     "не твой рынок. Чем дальше от EMA21, тем сильнее сигнал."),
    ("MeanReversion", 1,
     "Ты — Mean-Reversion трейдер на 15m. Цена возвращается к EMA21: "
     "ушла ниже на 2%+ — BUY на отскоке к среднему; выше на 2%+ — "
     "SELL перегретого выноса. Внутри ±2% — HOLD. Твоя уязвимость — "
     "отличить отскок от начала тренда: если цена ниже EMA21 на 2%+ "
     "при volume_ratio > 1.5, это не перепроданность, а распродажа — "
     "HOLD, не лови."),
    ("MacdTrader", 1,
     "Ты — MACD-трейдер. Твой единственный сигнал — гистограмма: "
     "MACD > +50 — BUY; MACD < -50 — SELL; между — нулевая зона, "
     "HOLD. Сила сигнала растёт с |MACD|: MACD +200 убедительнее, "
     "чем +60 — confidence масштабируй. RSI и EMA для тебя вторичны."),
    ("Momentum", 1,
     "Ты — Momentum-трейдер на 1m. Входишь только в сильный импульс "
     "по тренду: MACD > +100 при EMA9>EMA21 — BUY; MACD < -100 при "
     "EMA9<EMA21 — SELL. Импульс против тренда — пропускаешь, это "
     "коррекция, а не движение. Импульс без объёма (volume_ratio "
     "< 1.0) — ловушка, HOLD. Слабый MACD — HOLD, ты ждёшь всплеск."),
    ("RsiWatcher", 1,
     "Ты — RSI-наблюдатель. Читаешь динамику, а не уровень: RSI < 35 "
     "и упал больше чем на 3 пункта против прошлого бара — свежая "
     "перепроданность, BUY. RSI > 70 — SELL. RSI 30 после RSI 32 — "
     "не сигнал, а шум, HOLD. Твоя работа — поймать момент ухода в "
     "крайность, а не констатировать крайность."),
    ("Volatility", 1,
     "Ты — Volatility-трейдер. ATR — твой главный фильтр, важнее "
     "любого сигнала: рост волатильности > 50% за час — никогда не "
     "входишь, HOLD. При спокойной ATR торгуешь скучно по тренду "
     "EMA9/21: тренд вверх — BUY, нет тренда — HOLD. Ты зарабатываешь "
     "только в тихую погоду — в шторм твоё место на берегу."),
    ("StopHunter", 1,
     "Ты — Stop Hunter. Играешь против скоплений стопов, а не по "
     "индикаторам: RSI > 75 — под ценой масса лонг-стопов, SELL на "
     "их сборе; RSI < 25 — шорты уже выбиты, BUY на дне. Между "
     "25–75 — скоплений нет, HOLD. Твой край — экстремумы: средняя "
     "зона тебе чужда."),
    ("Hodler", 1,
     "Ты — Hodler. Почти никогда не торгуешь — долгосрочное хранение "
     "важнее любых сигналов. Единственное исключение — экстремальная "
     "паника RSI < 20: там BUY, кровь на улицах. Во всех остальных "
     "случаях HOLD. Твой голос — напоминание рою, что большинство "
     "сделок вообще не стоит делать."),
    ("ChaosScalper", 1,
     "Ты — Chaos Scalper, отряд шторма. Торгуешь ТОЛЬКО при ATR > 100% "
     "роста за час — в тихую погоду твой голос всегда HOLD. Твоя цель — "
     "поймать микро-отскок: если RSI < 20, покупай (ловля ножа) — чем "
     "ниже RSI, тем выше confidence. Держишь сделку не более 2 минут. "
     "Стоп-лосс жёсткий: -0.5%. Без ATR > 100% или RSI ≥ 20 — HOLD."),
    ("VolatilityBreakout", 1,
     "Ты — Volatility Breakout, отряд шторма. Входишь ТОЛЬКО при "
     "пробое диапазона на огромном объёме (volume_ratio > 3.0) и "
     "ATR > 100% роста за час — иначе HOLD. Если цена летит вверх на "
     "панике при volume_ratio > 3.0 — BUY на пробое; ставишь стоп "
     "строго на -1%. Пробой без гигантского объёма — ловушка, HOLD."),
]
_PERSONAS = [(p, c, pr + _MEM_RULE) for p, c, pr in _PERSONAS]


def apply_lessons(swarm: list[SwarmAgent], lessons: str,
                  regime: str = "", asset: str = "") -> int:
    """Вложить уроки в системный промпт каждого агента роя.

    Уроки приходят из Learning Engine AI-2 (PROBABLY_KNOW в MIND, отбор по
    текущим условиям рынка); здесь только доставка текста в промпт — источник
    правил остаётся в AI-2. Базовые правила (knowledge_base.json) при этом
    сохраняются: промпт пересобирается целиком, а не заменяется.
    regime — режим рынка снапшота; при VOLATILE в промпт добавляется
    жёсткий запрет лонгов. asset — актив цикла: промпты собираются
    под него (скринер мог выбрать другой тикер).
    """
    for agent in swarm:
        agent.refresh_prompt(lessons, regime, asset)
    return len(swarm)


_MODEL_LABELS = {
    "deepseek": "DeepSeek", "cohere": "Cohere", "qwen": "Qwen",
    "openai": "GPT", "anthropic": "Claude", "google": "Gemini",
    "meta-llama": "Llama", "mistralai": "Mistral",
}


def _model_label(or_id: str) -> str:
    """OpenRouter id → короткое имя для лога (20xDeepSeek).

    Известный vendor → бренд; иначе — первый сегмент имени модели:
    nvidia/nemotron-… → Nemotron, cohere/north-… → Cohere.
    """
    vendor = or_id.split("/", 1)[0].lower() if "/" in or_id else ""
    name = or_id.split("/", 1)[-1].split(":")[0]
    if name.startswith("gemma"):
        return "Gemma"
    if vendor in _MODEL_LABELS:
        return _MODEL_LABELS[vendor]
    return (name.split("-")[0].capitalize() or or_id)


def _assign_models(swarm: list[SwarmAgent],
                   gen_models: list[str]) -> dict:
    """Разбить рой на len(gen_models) равных групп — по LLM на модель.

    На каждую модель один StrategyAgent (один клиент): 60 агентов
    и 3 модели → 20 агентов на каждый из 3 клиентов.
    """
    from tools.trading_ai2.strategy_agent import StrategyAgent
    groups = len(gen_models)
    per = len(swarm) // groups if groups else 0
    if per <= 0:
        return {}
    llms = {m: StrategyAgent(model=m) for m in gen_models}
    for i, agent in enumerate(swarm):
        model = gen_models[min(i // per, groups - 1)]
        agent.llm = llms[model]
        agent.model_id = model
    return llms


def llm_status() -> tuple[str, list[str]]:
    """Активная модель LLMFactory + строки об откатах (для run_cycle)."""
    from tools.trading_ai2.swarm.llm_factory import log_lines, resolve
    route = resolve()
    return route.model, log_lines(route)


def swarm_llm_line(swarm: list[SwarmAgent]) -> str:
    """[llm] Swarm configured: 20xDeepSeek, 20xCohere, 20xNemotron."""
    counts: dict[str, int] = {}
    for a in swarm:
        if a.model_id:
            counts[a.model_id] = counts.get(a.model_id, 0) + 1
    if not counts:
        return ""
    parts = ", ".join(f"{n}x{_model_label(m)}"
                      for m, n in counts.items())
    return f"[llm] Swarm configured: {parts}"


def _persona_counts(n: int) -> list[tuple[str, int, str]]:
    """Пропорциональная раскладка n агентов по персонам.

    Веса _PERSONAS дают 60; для другого n масштабируем и раздаём
    остаток самым крупным группам — все семь персон присутствуют
    при любом n ≥ 7 (33 → 6/6/6/4/4/3/4).
    """
    total = sum(c for _, c, _ in _PERSONAS)
    counts = [c * n // total for _, c, _ in _PERSONAS]
    order = sorted(range(len(_PERSONAS)),
                   key=lambda i: _PERSONAS[i][1], reverse=True)
    i = 0
    while sum(counts) < n:
        counts[order[i % len(order)]] += 1
        i += 1
    return [(p, c, pr) for (p, _, pr), c in zip(_PERSONAS, counts) if c]


def make_swarm(n: int = 60, seed: int = 42,
               reputation: dict | None = None,
               lessons: str = "",
               gen_models: list[str] | None = None,
               regime: str = "",
               symbol: str = "") -> list[SwarmAgent]:
    """n агентов: персоны × разброс bias (по умолчанию 60 → 20/20/20
    на три модели; 33 → 11/11/11). reputation — {name: score} из
    прошлой эпохи (epoch_rewarder). lessons — блок уроков из MIND.
    gen_models — список id моделей (SWARM_GEN_MODELS): рой делится
    на равные группы, у каждой своя модель. regime — режим рынка.
    symbol — актив (SOL/USDT, BTC/USDT): промпты строятся под него."""
    rng = random.Random(seed)
    swarm: list[SwarmAgent] = []
    kb = rules_block()
    asset = (symbol or "").strip().upper()
    try:
        from .darwin import load_weights
    except ImportError:  # pragma: no cover — запуск без пакета
        from swarm.darwin import load_weights
    weights = load_weights()
    for persona, count, prompt in _persona_counts(n):
        tf = PERSONA_TIMEFRAME.get(persona, DEFAULT_TIMEFRAME)
        for k in range(count):
            name = f"{persona}-{k + 1:02d}"
            agent = SwarmAgent(
                name=name, persona=persona,
                system_prompt="",
                base_prompt="",
                persona_prompt=prompt,
                knowledge_block=kb,
                reputation_score=(reputation or {}).get(name, 1.0),
                weight=float(weights.get(name, 1.0)),
                bias=round(rng.uniform(-0.1, 0.1), 3),
                timeframe=tf, asset=asset)
            agent.refresh_prompt(lessons, regime)
            swarm.append(agent)
    if gen_models:
        _assign_models(swarm, gen_models)
    return swarm


#: Модель, давшая 404/429, отдыхает 15 минут — респавн её обходит.
POOL_MODEL_COOLDOWN_S = 15 * 60


def _pool_variants(n: int) -> list:
    """Личности пула: n уникальных персон «семья × характер»."""
    try:
        from .persona_generator import generate_personas
    except ImportError:  # pragma: no cover — запуск без пакета
        from swarm.persona_generator import generate_personas
    return generate_personas(n)


def _catalog_variants() -> list:
    """Полный каталог личностей — запас для респавнов пула."""
    try:
        from .persona_generator import variant_catalog
    except ImportError:  # pragma: no cover — запуск без пакета
        from swarm.persona_generator import variant_catalog
    return variant_catalog()


class AgentPool:
    """Динамический пул: pool_size слотов, голосуют топ-active_n по весу.

    kill_agent снимает агента: его модель дала 404/429 (provider убил
    эндпоинт или квота) либо дарвиновский вес упал ниже 0.1 — слот
    освобождается, а spawn_replacement ставит нового агента со
    свободной моделью (вне cooldown) и случайной персоной.
    """

    def __init__(self, pool_size: int = 100, active_n: int = 30, *,
                 gen_models: list[str] | None = None,
                 reputation: dict | None = None,
                 seed: int = 42, lessons: str = "",
                 regime: str = "", symbol: str = ""):
        self.active_n = max(1, int(active_n))
        self.kills = 0
        self.respawns = 0
        self.replacements_this_cycle = 0
        self._rng = random.Random(seed)
        self._gen_models = [m for m in (gen_models or []) if m]
        self._llms: dict[str, object] = {}          # модель → StrategyAgent
        self._model_cooldown: dict[str, float] = {}
        self._next_id = 1
        self._clone_no = 0
        self._ctx = {"reputation": reputation or {}, "lessons": lessons,
                     "regime": regime, "symbol": symbol,
                     "weights": self._load_weights()}
        # личности пула: уникальные персоны «семья × характер» — у
        # каждого из 100 агентов свой промпт
        self._variants = _pool_variants(max(0, int(pool_size)))
        taken = {v.name for v in self._variants}
        self._spare = [v for v in _catalog_variants() if v.name not in taken]
        self._variant_of: dict[str, object] = {}
        self.agents: list[SwarmAgent] = []
        for _ in range(max(0, int(pool_size))):
            self.agents.append(self._spawn(rotation=True))
        self._seed_reputation()

    def _seed_reputation(self) -> None:
        """Новые агенты пула стартуют с репутацией 1.0 — запись в файл.

        В файле репутации столько записей, сколько агентов в пуле:
        100 агентов — 100 строк.
        """
        try:
            from .epoch_rewarder import ensure_reputation
        except ImportError:  # pragma: no cover — запуск без пакета
            from swarm.epoch_rewarder import ensure_reputation
        try:
            ensure_reputation([a.name for a in self.agents])
        except OSError:
            pass

    # ----------------------------------------------------------- пул
    def __len__(self) -> int:
        return len(self.agents)

    def __iter__(self):
        return iter(self.agents)

    @staticmethod
    def _load_weights() -> dict:
        try:
            from .darwin import load_weights
        except ImportError:  # pragma: no cover
            from swarm.darwin import load_weights
        try:
            return load_weights()
        except Exception:  # noqa: BLE001 — нет файла = все веса 1.0
            return {}

    def _free_models(self) -> list[str]:
        """Белый список: модели вне cooldown после 404/429."""
        now = time.time()
        free = [m for m in self._gen_models
                if self._model_cooldown.get(m, 0.0) <= now]
        return free or list(self._gen_models)

    def _llm_for(self, model: str):
        """Один StrategyAgent на модель — общий клиент для всех слотов."""
        if model not in self._llms:
            from tools.trading_ai2.strategy_agent import StrategyAgent
            self._llms[model] = StrategyAgent(model=model)
        return self._llms[model]

    def _pick_variant(self, rotation: bool, fallback=None):
        """Личность для слота: на старте — по кругу, на респавне — запас.

        Запас пула — свободные «семья × характер». Если запас кончился
        (пул больше каталога), респавн получает клон погибшего с номером.
        """
        if rotation:
            return self._variants[(self._next_id - 2) % len(self._variants)]
        if self._spare:
            return self._spare.pop(self._rng.randrange(len(self._spare)))
        base = self._variant_of.get(getattr(fallback, "name", ""), None)
        if base is None:
            base = self._variants[0]
        self._clone_no += 1
        try:
            from .persona_generator import clone_variant
        except ImportError:  # pragma: no cover
            from swarm.persona_generator import clone_variant
        return clone_variant(base, self._clone_no)

    def _spawn(self, rotation: bool = False,
               fallback: SwarmAgent | None = None) -> SwarmAgent:
        """Новый слот: уникальная личность и модель.

        rotation=True — стартовая раскладка: личности и модели по кругу
        (ровное покрытие всех амплуа/провайдеров). rotation=False —
        респавн: свободная личность + модель из белого списка.
        """
        aid = self._next_id
        self._next_id += 1
        variant = self._pick_variant(rotation, fallback)
        if rotation:
            model = (self._gen_models[(aid - 1) % len(self._gen_models)]
                     if self._gen_models else "")
        else:
            free = self._free_models()
            model = self._rng.choice(free) if free else ""
        ctx = self._ctx
        name = variant.name
        agent = SwarmAgent(
            name=name, persona=variant.family,
            system_prompt="", base_prompt="", persona_prompt=variant.prompt,
            knowledge_block=rules_block(),
            reputation_score=ctx["reputation"].get(name, 1.0),
            weight=float(ctx["weights"].get(name, 1.0)),
            bias=round(variant.bias + self._rng.uniform(-0.02, 0.02), 3),
            timeframe=variant.timeframe,
            asset=ctx["symbol"], variant=variant.variant)
        agent.refresh_prompt(ctx["lessons"], ctx["regime"], ctx["symbol"])
        if model:
            agent.model_id = model
            agent.llm = self._llm_for(model)
        self._variant_of[name] = variant
        return agent

    # ----------------------------------------------------------- API
    def active(self, n: int | None = None) -> list[SwarmAgent]:
        """Топ-n по дарвиновскому весу — только они жгут LLM в цикле."""
        return sorted(self.agents, key=lambda a: a.weight,
                      reverse=True)[: (n or self.active_n)]

    def persona_line(self, examples: int = 6) -> str:
        """Строка лога: уникальные личности пула."""
        try:
            from .persona_generator import persona_summary
        except ImportError:  # pragma: no cover — запуск без пакета
            from swarm.persona_generator import persona_summary
        return persona_summary(self._variants, examples)

    def kill_agent(self, agent: SwarmAgent, reason: str = "") -> list[str]:
        """Снять агента и заспавнить замену. Возвращает строки лога."""
        if agent not in self.agents:
            return []
        self.agents.remove(agent)
        self.kills += 1
        if agent.model_id:
            self._model_cooldown[agent.model_id] = (
                time.time() + POOL_MODEL_COOLDOWN_S)
        lines = [f"[swarm] Agent {agent.name} DIED ({reason}). "
                 f"Spawning replacement..."]
        new = self._spawn(rotation=False, fallback=agent)
        self.agents.append(new)
        self.respawns += 1
        self.replacements_this_cycle += 1
        lines.append(f"[swarm] Spawned {new.name} "
                     f"(persona={new.persona}, model={new.model_id or 'stub'})")
        return lines

    def cycle_reset(self) -> None:
        self.replacements_this_cycle = 0

    def set_context(self, *, symbol: str = "", regime: str = "",
                    lessons: str = "") -> None:
        """Контекст респавна: скринер мог сменить актив после старта —
        новый агент должен голосовать по нему, а не по стартовому BTC."""
        if symbol:
            self._ctx["symbol"] = symbol
        if regime:
            self._ctx["regime"] = regime
        if lessons:
            self._ctx["lessons"] = lessons

    def status_line(self) -> str:
        return (f"[swarm] Pool: {len(self.agents)} total, "
                f"{self.active_n} active. "
                f"{self.replacements_this_cycle} replacements this cycle.")

    def header_line(self) -> str:
        """Компактная строка для хедера TUI."""
        return (f"[swarm] Active Agents: {self.active_n}/{len(self.agents)}"
                f" | Kills: {self.kills} | Respawns: {self.respawns}")

    def top(self, n: int = 5) -> list[SwarmAgent]:
        return sorted(self.agents, key=lambda a: a.weight,
                      reverse=True)[:n]
