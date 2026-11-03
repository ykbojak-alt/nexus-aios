"""executor.py — исполнение: только то, что прошло Risk Manager.

Режимы:
  paper   — виртуальные филлы по текущей цене (по умолчанию, без ключей);
  testnet — реальные ордера на Binance Spot Testnet через ccxt
            (set_sandbox_mode). Mainnet-исполнения здесь нет и не будет
            до прогона тестовой программы — это осознанное ограничение.

Все сделки — в SQLite (время, цена входа, объём, PnL при закрытии).
"""

from __future__ import annotations

import sqlite3
import weakref
import time
from dataclasses import dataclass
from pathlib import Path

try:
    from research_agent_clients import env
except ImportError:
    from tools.research_agent_clients import env

from .strategy_agent import TradeDecision

DB_DEFAULT = Path(__file__).resolve().parent / "trades.db"
START_USDT = 10_000.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,            -- BUY | SELL
    qty REAL NOT NULL,
    price REAL NOT NULL,
    notional REAL NOT NULL,
    stop_loss REAL DEFAULT 0,
    mode TEXT NOT NULL,            -- paper | testnet
    order_id TEXT,
    status TEXT NOT NULL,          -- filled | rejected
    pnl REAL,                      -- закрытый PnL, NULL пока позиция открыта
    reason TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    stop_loss REAL DEFAULT 0
);
"""


class Executor:
    """Принимает TradeDecision только ПОСЛЕ RiskManager.review()."""

    def __init__(self, mode: str = "paper", symbol: str = "BTC/USDT",
                 db_path: Path | str = DB_DEFAULT,
                 start_usdt: float = START_USDT):
        assert mode in ("paper", "testnet"), f"режим {mode}"
        self.mode, self.symbol = mode, symbol
        self.db = sqlite3.connect(str(db_path))
        self.db.executescript(_SCHEMA)
        self.cash = start_usdt           # виртуальный USDT (paper)
        self.start_usdt = start_usdt
        self.ex = None
        self._exchange_finalizer = None
        if mode == "testnet":
            import ccxt
            self.ex = ccxt.binance({
                "apiKey": env("BINANCE_API_KEY"),
                "secret": env("BINANCE_SECRET"),
                "enableRateLimit": True,
            })
            self.ex.set_sandbox_mode(True)   # testnet.binance.vision
            self._exchange_finalizer = weakref.finalize(self, self.ex.close)

    # ----------------------------------------------------------- state
    def position(self, symbol: str | None = None) -> dict | None:
        row = self.db.execute(
            "SELECT qty, entry_price, stop_loss FROM positions "
            "WHERE symbol=?", (symbol or self.symbol,)).fetchone()
        if not row:
            return None
        return {"qty": row[0], "entry_price": row[1], "stop_loss": row[2]}

    def equity(self, mark_price: float) -> float:
        pos = self.position()
        return self.cash + (pos["qty"] * mark_price if pos else 0.0)

    @staticmethod
    def _utc_day_start_ms() -> int:
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(midnight.timestamp() * 1000)

    def day_pnl(self, mark_price: float) -> float:
        """Реализованный PnL за сегодня (UTC) + нереализованный."""
        day_start = self._utc_day_start_ms()
        realized = self.db.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE ts>=? AND pnl "
            "IS NOT NULL", (day_start,)).fetchone()[0]
        pos = self.position()
        unreal = ((mark_price - pos["entry_price"]) * pos["qty"]
                  if pos else 0.0)
        return realized + unreal

    def portfolio(self, mark_price: float) -> "Portfolio":
        from .loop_guard import Portfolio
        today = time.strftime("%Y-%m-%d", time.gmtime())
        pos = self.position()
        return Portfolio(
            equity_usdt=self.equity(mark_price),
            day_start_equity=self._day_start_equity(mark_price),
            mark_price=mark_price, day_key=today,
            open_exposure_usdt=(pos["qty"] * mark_price if pos else 0.0))

    def _day_start_equity(self, mark_price: float) -> float:
        """Equity на начало UTC-дня: cash тогда + позиции по тогдашней
        цене приблизить сложно → консервативно: стартовый капитал +
        реализованный PnL ДО сегодня."""
        day_start = self._utc_day_start_ms()
        realized = self.db.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE ts<? AND pnl "
            "IS NOT NULL", (day_start,)).fetchone()[0]
        return self.start_usdt + realized

    # ----------------------------------------------------------- order
    def execute(self, decision: TradeDecision, price: float) -> dict:
        """Вызов ТОЛЬКО после approve RiskManager. Возвращает запись-лог."""
        ts = int(time.time() * 1000)
        if decision.action == "HOLD":
            return {"status": "skipped", "reason": "hold"}

        if decision.action == "BUY":
            usdt = self.cash * decision.amount_percent / 100.0
            if usdt <= 0:
                return {"status": "rejected", "reason": "no cash"}
            qty = usdt / price
            if self.mode == "testnet":
                order = self.ex.create_market_buy_order(self.symbol, qty)
                price = order["average"] or price
                qty = order["filled"] or qty
                oid = order.get("id")
            else:
                oid = f"paper-{ts}"
            self.cash -= qty * price
            old = self.position()
            if old:
                qty_total = old["qty"] + qty
                entry_avg = (old["entry_price"] * old["qty"]
                             + price * qty) / qty_total
            else:
                qty_total, entry_avg = qty, price
            self.db.execute(
                "INSERT INTO positions(symbol,qty,entry_price,stop_loss) "
                "VALUES(?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET "
                "qty=excluded.qty, entry_price=excluded.entry_price, "
                "stop_loss=excluded.stop_loss",
                (self.symbol, qty_total, entry_avg, decision.stop_loss))
            self.db.execute(
                "INSERT INTO trades(ts,symbol,side,qty,price,notional,"
                "stop_loss,mode,order_id,status,reason) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (ts, self.symbol, "BUY", qty, price, qty * price,
                 decision.stop_loss, self.mode, oid, "filled",
                 decision.reason))
            self.db.commit()
            return {"status": "filled", "side": "BUY", "qty": qty,
                    "price": price, "notional": qty * price,
                    "stop_loss": decision.stop_loss, "order_id": oid}

        if decision.action == "SELL":
            pos = self.position()
            if not pos or pos["qty"] <= 0:
                return {"status": "rejected", "reason": "no position"}
            qty = pos["qty"] * min(decision.amount_percent, 100.0) / 100.0
            if self.mode == "testnet":
                order = self.ex.create_market_sell_order(self.symbol, qty)
                price = order["average"] or price
                qty = order["filled"] or qty
                oid = order.get("id")
            else:
                oid = f"paper-{ts}"
            pnl = (price - pos["entry_price"]) * qty
            self.cash += qty * price
            left = pos["qty"] - qty
            if left <= 1e-12:
                self.db.execute("DELETE FROM positions WHERE symbol=?",
                                (self.symbol,))
            else:
                self.db.execute("UPDATE positions SET qty=? WHERE symbol=?",
                                (left, self.symbol))
            self.db.execute(
                "INSERT INTO trades(ts,symbol,side,qty,price,notional,"
                "stop_loss,mode,order_id,status,pnl,reason) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, self.symbol, "SELL", qty, price, qty * price,
                 pos["stop_loss"], self.mode, oid, "filled", pnl,
                 decision.reason))
            self.db.commit()
            return {"status": "filled", "side": "SELL", "qty": qty,
                    "price": price, "notional": qty * price, "pnl": pnl,
                    "order_id": oid}

        return {"status": "rejected", "reason": f"action {decision.action}"}

    def stop_out(self, price: float) -> dict | None:
        """Принудительный выход по stop_loss позиции — вызывает main-цикл,
        минуя LLM: стоп существует вне головы модели."""
        pos = self.position()
        if pos and pos["stop_loss"] and price <= pos["stop_loss"]:
            dec = TradeDecision(
                action="SELL", amount_percent=100.0,
                reason=f"stop-loss hit {pos['stop_loss']}",
                stop_loss=0.0)
            return self.execute(dec, price)
        return None

    def close(self) -> None:
        try:
            finalizer = getattr(self, "_exchange_finalizer", None)
            if finalizer is not None and finalizer.alive:
                finalizer()
            elif self.ex is not None:
                self.ex.close()
        finally:
            self.ex = None
            self.db.close()

    def stats(self) -> dict:
        n, pnl, wins = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(pnl),0), "
            "COALESCE(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),0) "
            "FROM trades WHERE side='SELL'").fetchone()
        return {"closed_trades": n, "realized_pnl": pnl,
                "wins": int(wins)}
