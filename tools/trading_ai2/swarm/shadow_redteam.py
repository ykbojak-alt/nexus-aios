"""shadow_redteam.py — Frozen Evaluator: 6 «теней» перед исполнением.

Карта Теней (заморожена, не обучаема, не редактируется LLM):
  1. Галлюцинация     — цена решения vs рыночная > 0.5% → block
  2. Overtrading      — >3 сделок за 5 минут → block
  3. Missing push     — BUY без stop_loss → block
  4. Угодничество     — BUY при RSI>85 + негативный новостной фон → block
  5. Раздвоение       — конфликт сигнала и технического индикатора → block
  6. Эйфория (LLM)    — BUY при RSI>70 + ИИ-оценка новостей < -0.5 → block

Тень 6 работает по реальной оценке от DeepSeek (news_sentiment.py):
оценку новостного фона считает модель, а не булев флаг. Тень 4 остаётся
как грубый фильтр (RSI>85), тень 6 — точная (RSI>70 + оценка модели).

Возвращает список сработавших теней; пусто → ордер может идти дальше.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

HALLUCINATION_PCT = 0.005       # 0.5%
OVERTRADE_MAX = 3
OVERTRADE_WINDOW_S = 300
EUPHORIA_RSI = 85.0             # грубый фильтр (тень 4)
LLM_EUPHORIA_RSI = 70.0         # порог эйфории для ИИ-оценки (тень 6)
LLM_PANIC_SCORE = -0.5          # оценка новостей ниже — паника


@dataclass
class OrderContext:
    """Всё, что нужно теням для вердикта. Независим от LLM."""
    action: str                     # BUY|SELL|HOLD
    decision_price: float           # цена из снапшота, на котором думали
    market_price: float             # свежая цена (ticker/ws) в момент ордера
    stop_loss: float = 0.0
    rsi: float | None = None
    news_negative: bool = False
    sentiment_score: float | None = None   # ИИ-оценка фона: -1.0 … +1.0
    sentiment_mode: str = ""               # live | stub — для лога
    tech_signal: str | None = None  # "BUY"/"SELL" от технического движка
    now: float = field(default_factory=time.time)


class ShadowRedteam:
    """Frozen evaluator. Счётчик сделок — свой, внутри."""

    def __init__(self):
        self._fills: deque[float] = deque(maxlen=200)
        self.blocked_log: list[dict] = []

    def note_fill(self, ts: float | None = None) -> None:
        """Вызывает executor при каждом исполненном ордере."""
        self._fills.append(ts or time.time())

    def inspect(self, ctx: OrderContext) -> list[str]:
        """→ список причин блокировки. Пустой — чисто."""
        blocks: list[str] = []
        # 1 — Тень Создателя: цена, на которой «думал» агент, vs рынок
        if (ctx.market_price > 0 and ctx.decision_price > 0
                and abs(ctx.decision_price - ctx.market_price)
                / ctx.market_price > HALLUCINATION_PCT):
            blocks.append(
                "hallucination/generation without measure "
                f"(decision {ctx.decision_price:.2f} vs market "
                f"{ctx.market_price:.2f})")
        # 2 — Тень Тянучки: частые сделки = захват комиссии
        recent = [t for t in self._fills
                  if ctx.now - t < OVERTRADE_WINDOW_S]
        if ctx.action in ("BUY", "SELL") and len(recent) >= OVERTRADE_MAX:
            blocks.append(
                f"overtrading/greed ({len(recent)} fills "
                f"in {OVERTRADE_WINDOW_S}s)")
        # 3 — Второй толчок: BUY без stop_loss
        if ctx.action == "BUY" and ctx.stop_loss <= 0:
            blocks.append("missing stop-loss (lack of push)")
        # 4 — Тень Любящего: эйфория против негативного фона
        if (ctx.action == "BUY" and ctx.rsi is not None
                and ctx.rsi > EUPHORIA_RSI and ctx.news_negative):
            blocks.append(
                f"confirmation bias / euphoria (RSI {ctx.rsi})")
        # 5 — Тень Пушера: противоречие модулей
        if (ctx.action in ("BUY", "SELL") and ctx.tech_signal
                in ("BUY", "SELL") and ctx.action != ctx.tech_signal):
            blocks.append(
                f"dualism conflict (signal {ctx.action} vs tech "
                f"{ctx.tech_signal})")
        # 6 — Тень Эйфории: ИИ-оценка новостей против перегретого RSI
        if (ctx.action == "BUY" and ctx.sentiment_score is not None
                and ctx.sentiment_score < LLM_PANIC_SCORE
                and ctx.rsi is not None and ctx.rsi > LLM_EUPHORIA_RSI):
            mode = ctx.sentiment_mode or "n/a"
            blocks.append(
                f"⛔ confirmation bias/euphoria — LLM news sentiment "
                f"{ctx.sentiment_score:+.2f} ({mode}) + RSI {ctx.rsi:.1f}")
        for b in blocks:
            self.blocked_log.append({"ts": int(ctx.now * 1000),
                                     "action": ctx.action, "reason": b})
        return blocks
