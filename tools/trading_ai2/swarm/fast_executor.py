"""fast_executor.py — быстрое исполнение консенсуса.

Никаких контрактов: только ccxt → Binance Testnet (или paper) через
родительский Executor. Два замороженных гейта до биржи:
  1. ShadowRedteam — 6 «теней» (галлюцинация, overtrading, стоп,
     эйфория, раздвоение, ИИ-эйфория) — не обучаем, не обманываем;
  2. RiskManager — лимиты 2%/5%/stop-loss.
Стоп-лосс позиции проверяется на каждом тике вне LLM.

Оценка новостного фона (news_sentiment.py) приходит снаружи: кэш
запросов — у модуля, горячий путь ордера сети не трогает.
"""

from __future__ import annotations

import time

from ..executor import Executor
from ..loop_guard import RiskManager
from ..strategy_agent import TradeDecision
from .arbiter import Consensus
from .shadow_redteam import ShadowRedteam, OrderContext


class FastExecutor:
    """Consensus → shadows → guard → market order. Миллисекунды."""

    def __init__(self, mode: str = "paper", symbol: str = "BTC/USDT",
                 db_path=":memory:", start_usdt: float = 10_000.0):
        self.shadow = ShadowRedteam()       # заморожен, вне досягаемости LLM
        self.guard = RiskManager()
        self.ex = Executor(mode=mode, symbol=symbol, db_path=db_path,
                           start_usdt=start_usdt)

    def check_stop(self, price: float) -> dict | None:
        return self.ex.stop_out(price)

    def execute(self, con: Consensus, price: float, *,
                market_price: float | None = None,
                rsi: float | None = None,
                news_negative: bool = False,
                sentiment_score: float | None = None,
                sentiment_mode: str = "",
                tech_signal: str | None = None) -> dict:
        if not con.actionable:
            return {"status": "skipped", "reason": con.reason}
        decision = TradeDecision(
            action=con.action, amount_percent=con.amount_percent,
            reason=f"swarm: {con.reason}", stop_loss=con.stop_loss)

        # ── слой 0: тени — до guard, до биржи ─────────────────────
        blocks = self.shadow.inspect(OrderContext(
            action=con.action,
            decision_price=price,
            market_price=market_price if market_price is not None
            else price,
            stop_loss=con.stop_loss, rsi=rsi,
            news_negative=news_negative,
            sentiment_score=sentiment_score,
            sentiment_mode=sentiment_mode,
            tech_signal=tech_signal))
        if blocks:
            return {"status": "shadow_blocked", "reasons": blocks}

        # ── слой 1: risk manager ──────────────────────────────────
        review = self.guard.review(decision, self.ex.portfolio(price))
        if not review.approved:
            return {"status": "blocked", "reason": review.reason}

        res = self.ex.execute(decision, price)
        if res.get("status") == "filled":
            self.shadow.note_fill()
        return {"guard": "passed", **res}

    def execute_verdict(self, con: Consensus, emit=print,
                        equity_usd: float = 10_000.0, *,
                        price: float | None = None,
                        rsi: float | None = None,
                        sentiment_score: float | None = None,
                        sentiment_mode: str = "",
                        tech_signal: str | None = None) -> dict:
        """Контракт исполнителя (общий с FundingExecutor): вердикт → ордер.

        Arbiter зовёт именно его — MarginPad и ccxt-путь неразличимы
        для вызывающего.
        """
        if not getattr(con, "approved", con.actionable):
            return {"status": "skipped", "reason": con.reason}
        if price is None:
            from ..market_data import MarketData
            price = float(MarketData(symbol=self.symbol).snapshot()["price"])
        res = self.execute(con, price, market_price=price, rsi=rsi,
                           sentiment_score=sentiment_score,
                           sentiment_mode=sentiment_mode,
                           tech_signal=tech_signal)
        if res.get("status") == "filled":
            emit(f"    [execution] filled {res.get('side')} "
                 f"{res.get('qty', 0):.6f} @ {res.get('price', 0):.2f} "
                 f"(paper/testnet)")
        return res
