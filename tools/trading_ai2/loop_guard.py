"""loop_guard.py — Risk Manager: обязательный gate перед биржей.

LLM не имеет права вызывать API биржи в обход этого модуля. Каждое
решение проходит review(): лимиты позиции, дневной убыток, обязательный
stop-loss. Нарушение → блокировка с причиной в лог.

Лимиты (defaults, переопределяются env TRADING_*):
  max_position_pct = 2% депозита на сделку;
  max_daily_loss_pct = 5% — стоп торговли до следующего UTC-дня;
  require_stop_loss = обязательный стоп на каждый BUY.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

try:
    from research_agent_clients import env
except ImportError:
    from tools.research_agent_clients import env

from .strategy_agent import TradeDecision


@dataclass
class RiskConfig:
    max_position_pct: float = 2.0      # % депозита на сделку
    max_daily_loss_pct: float = 5.0    # % от equity на начало дня
    require_stop_loss: bool = True

    @classmethod
    def from_env(cls) -> "RiskConfig":
        def _f(name: str, default: float) -> float:
            try:
                return float(env(name) or default)
            except ValueError:
                return default
        return cls(
            max_position_pct=_f("TRADING_MAX_POSITION_PCT", 2.0),
            max_daily_loss_pct=_f("TRADING_MAX_DAILY_LOSS_PCT", 5.0),
            require_stop_loss=(env("TRADING_REQUIRE_STOP_LOSS") or "1")
            not in ("0", "false"))


@dataclass
class ReviewResult:
    approved: bool
    reason: str = ""
    clamped: bool = False


@dataclass
class Portfolio:
    """Состояние, которое Risk Manager видит (заполняет executor)."""
    equity_usdt: float
    day_start_equity: float
    mark_price: float = 0.0              # текущая цена инструмента
    day_key: str = ""                    # UTC-дата, например "2026-10-25"
    open_exposure_usdt: float = 0.0


class RiskManager:
    """Перехватывает TradeDecision до исполнения. Без «мягких» поправок:
    нарушил лимит — сделка не существует, точка."""

    def __init__(self, config: RiskConfig | None = None):
        self.cfg = config or RiskConfig.from_env()
        self.halted_day: str | None = None
        self.blocked_log: list[dict] = []

    def _block(self, decision: TradeDecision, reason: str) -> ReviewResult:
        self.blocked_log.append({"ts": int(time.time() * 1000),
                                 "action": decision.action,
                                 "reason": reason})
        return ReviewResult(approved=False, reason=reason)

    def review(self, decision: TradeDecision,
               pf: Portfolio) -> ReviewResult:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if self.halted_day == today:
            return self._block(decision, "daily loss limit — halted today")

        # дневной убыток от начала дня
        if pf.day_key == today and pf.day_start_equity > 0:
            dd = (pf.day_start_equity - pf.equity_usdt) \
                / pf.day_start_equity * 100.0
            if dd >= self.cfg.max_daily_loss_pct:
                self.halted_day = today
                return self._block(
                    decision,
                    f"daily loss {dd:.2f}% ≥ {self.cfg.max_daily_loss_pct}%"
                    " — halt until tomorrow")

        if decision.action == "HOLD":
            return ReviewResult(approved=True, reason="hold — no-op")

        if decision.action == "BUY":
            if self.cfg.require_stop_loss and decision.stop_loss <= 0:
                return self._block(decision, "BUY без stop_loss")
            if pf.equity_usdt <= 0:
                return self._block(decision, "equity = 0")
            notional = pf.equity_usdt * decision.amount_percent / 100.0
            cap = pf.equity_usdt * self.cfg.max_position_pct / 100.0
            if notional > cap:
                return self._block(
                    decision,
                    f"exceeding 2% risk limit "
                    f"(want {notional:.2f} > cap {cap:.2f} USDT)")
            # стоп ниже текущей цены для BUY — иначе это не стоп, а ловушка
            if (pf.mark_price > 0
                    and decision.stop_loss >= pf.mark_price):
                return self._block(
                    decision,
                    f"stop_loss {decision.stop_loss} "
                    f"≥ price {pf.mark_price} — стоп не ниже входа")

        return ReviewResult(approved=True, reason="ok")
