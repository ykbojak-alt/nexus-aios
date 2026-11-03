"""funding_executor.py — боевое исполнение funding-арбитража.

MarginPad Trading API (https://marginpad.io/trading-api/) — бесплатный
симулятор на реальных ценах, без KYC, чистый REST:
  GET  /api/bot/v1/price?symbol=BTC      — публичные котировки
  POST /api/bot/v1/open                  — X-API-Key + JSON-тело
  POST /api/bot/v1/close                 — закрытие по position_id
  GET  /api/bot/v1/positions             — открытые позиции

Дельта-нейтральная эмуляция: LONG margin_usd + SHORT margin_usd на
одинаковом символе → нулевая дельта, фандинг-эквивалент собираем как
PnL-спред пары. Плечо передаётся в теле запроса.

Shadow-пропуск: action="HEDGE"; галлюцинация сверяет decision_price
со свежим GET /price (реальный ответ API), overtrading — счётчик филов.
Без MARGINPAD_API_KEY — честный DRY-RUN.
"""

from __future__ import annotations

import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from research_agent_clients import env
except ImportError:
    from tools.research_agent_clients import env

from tools.trading_ai2.quant.funding_arb import (
    funding_signal, FUNDING_THRESHOLD)
from tools.trading_ai2.swarm.shadow_redteam import (
    ShadowRedteam, OrderContext)

API = "https://marginpad.io/api/bot/v1"
SYMBOL = "BTC"


_UA = {"User-Agent": "nexus-trading/1.0 (+https://devin.ai)",
       "Accept": "application/json"}


def _get(path: str) -> dict:
    req = urllib.request.Request(f"{API}{path}", headers=dict(_UA))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as error:
        # HTTPError is also a response object, but urlopen raises it before
        # the response context manager can take ownership.
        error.close()
        raise


def _post(path: str, body: dict, key: str) -> dict:
    req = urllib.request.Request(
        f"{API}{path}", data=json.dumps(body).encode(),
        method="POST",
        headers={**_UA, "Content-Type": "application/json",
                 "X-API-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        with e:
            return {"error": f"http_{e.code}",
                    "body": e.read().decode()[:300]}


def get_price(symbol: str = SYMBOL) -> dict:
    return _get(f"/price?symbol={symbol}")


def open_position(symbol: str, side: str, margin_usd: float,
                  leverage: int, key: str,
                  trail_pct: float = 1.5) -> dict:
    return _post("/open", {"symbol": symbol, "side": side,
                           "margin_usd": margin_usd,
                           "leverage": leverage,
                           "trail_pct": trail_pct}, key)


def close_position(position_id: str, key: str) -> dict:
    return _post("/close", {"id": position_id}, key)


def list_positions(key: str) -> dict:
    req = urllib.request.Request(f"{API}/positions",
                                 headers={**_UA, "X-API-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        with e:
            return {"error": f"http_{e.code}"}


def get_balance(key: str) -> dict:
    """GET /balance — equity/free/margin/unrealized_pnl счёта."""
    req = urllib.request.Request(f"{API}/balance",
                                 headers={**_UA, "X-API-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        with e:
            return {"error": f"http_{e.code}"}


class FundingExecutor:
    def __init__(self, margin_usd: float = 100.0, leverage: int = 3,
                 symbol: str = SYMBOL):
        self.margin_usd, self.leverage = margin_usd, leverage
        self.symbol = symbol          # базовый тикер: BTC, ETH, SOL…
        self.shadow = ShadowRedteam()
        self.key = env("MARGINPAD_API_KEY")
        self.live = bool(self.key)

    def run_once(self, emit=print,
                 min_rate: float = FUNDING_THRESHOLD,
                 funding_rate: float | None = None) -> dict:
        # ── feed: реальный /price ──────────────────────────────────
        try:
            p1 = get_price(self.symbol)
            price = float(p1["price"])
        except Exception as e:            # noqa: BLE001
            emit(f"    [funding] feed error: {e}")
            return {"status": "feed_error"}
        # funding у MarginPad нет — используем внешнюю ставку (передаёт
        # вызывающий; по умолчанию 0 → честный no_signal)
        rate = funding_rate if funding_rate is not None else 0.0
        emit(f"    [data] {self.symbol} {price:.2f} "
             f"chg24h={p1.get('change_24h_pct', 0):+.2f}% "
             f"funding_ext={rate * 100:.4f}%")

        if not funding_signal(rate, min_rate):
            emit(f"    [funding] rate {rate * 100:.4f}% ≤ "
                 f"{min_rate * 100:.2f}% — no arb")
            return {"status": "no_signal", "rate": rate}

        # ── shadow 1: галлюцинация — свежий /price прямо перед входом
        try:
            fresh = float(get_price(self.symbol)["price"])
        except Exception:                 # noqa: BLE001
            fresh = price
        blocks = self.shadow.inspect(OrderContext(
            action="HEDGE", decision_price=price, market_price=fresh))
        if blocks:
            for b in blocks:
                emit(f"    [shadow] ⛔ Execution BLOCKED. Reason: {b}")
            return {"status": "shadow_blocked", "reasons": blocks}

        if not self.live:
            emit(f"    [funding] DRY-RUN (нет MARGINPAD_API_KEY): "
                 f"Delta-neutral. LONG {self.margin_usd}$ + "
                 f"SHORT {self.margin_usd}$ @ ~{price:.2f} "
                 f"lev={self.leverage}x")
            return {"status": "dry_run", "price": price}

        # ── исполнение: две ноги, дельта = 0 ─────────────────────────
        long_r = open_position(self.symbol, "long", self.margin_usd,
                               self.leverage, self.key)
        short_r = open_position(self.symbol, "short", self.margin_usd,
                                self.leverage, self.key)
        self.shadow.note_fill(); self.shadow.note_fill()
        lid = (long_r.get("position") or {}).get("id", "?")
        sid = (short_r.get("position") or {}).get("id", "?")
        emit(f"    [execution] Position opened on MarginPad: "
             f"{lid} (LONG) + {sid} (SHORT) @ {price:.2f} "
             f"lev={self.leverage}x")
        return {"status": "filled", "price": price,
                "long": long_r, "short": short_r}

    # ------------------------------------------------ arbiter path
    def execute_verdict(self, con, emit=print,
                        equity_usd: float = 10_000.0, *,
                        rsi: float | None = None,
                        sentiment_score: float | None = None,
                        sentiment_mode: str = "",
                        tech_signal: str | None = None) -> dict:
        """Вердикт Арбитра → направленный ордер MarginPad.

        BUY → long, SELL → short; margin = equity × amount_percent
        (≤2% депозита — размер уже урезан Генератором). Трейлинг-стоп
        MarginPad (trail_pct) играет роль stop-loss. Тени проверяются
        до отправки — галлюцинация сверяет цену вердикта со свежим
        GET /price, а RSI/оценка новостей идут в ту же тень, что и на
        ccxt-пути (общий контракт исполнителя).
        """
        if not getattr(con, "approved", getattr(con, "actionable", False)):
            return {"status": "skipped",
                    "reason": getattr(con, "reason", "not actionable")}
        side = "long" if con.action == "BUY" else "short"
        margin = round(equity_usd * con.amount_percent / 100.0, 2)
        # CHAOS MODE (risk_mode=MICRO): позиция режется до микро-лота —
        # не более 0.5% эквити вместо стандартных 2–5%.
        micro = getattr(con, "risk_mode", "NORMAL") == "MICRO"
        if micro:
            margin = min(margin, round(equity_usd * 0.5 / 100.0, 2))

        exec_symbol = self.symbol
        try:
            price = float(get_price(exec_symbol)["price"])
        except Exception as e:            # noqa: BLE001
            if exec_symbol == SYMBOL:
                emit(f"    [funding] feed error: {e}")
                return {"status": "feed_error"}
            try:                        # MarginPad не знает символ → BTC
                price = float(get_price(SYMBOL)["price"])
                emit(f"    [execution] MarginPad supports only BTC. "
                     f"Falling back to BTC. ({exec_symbol}: {e})")
                exec_symbol = SYMBOL
            except Exception:
                emit(f"    [funding] feed error: {e}")
                return {"status": "feed_error"}

        blocks = self.shadow.inspect(OrderContext(
            action=con.action, decision_price=price,
            market_price=price, stop_loss=con.stop_loss, rsi=rsi,
            sentiment_score=sentiment_score, sentiment_mode=sentiment_mode,
            tech_signal=tech_signal))
        if blocks:
            for b in blocks:
                emit(f"    [shadow] ⛔ Execution BLOCKED. Reason: {b}")
            return {"status": "shadow_blocked", "reasons": blocks}

        if not self.live:
            emit(f"    [execution] DRY-RUN: {side.upper()} "
                 f"{exec_symbol} margin ${margin} lev={self.leverage}x "
                 f"@ ~{price:.2f}")
            return {"status": "dry_run", "side": side}

        # Slippage Guard: в шторм стакан может быть пустым. Спред
        # шире 0.3% — сделка отменяется до отправки ордера.
        if micro:
            spread = self._spread_pct(exec_symbol)
            if spread is not None and spread > 0.3:
                emit(f"    [execution] Order cancelled: spread too "
                     f"wide ({spread:.1f}%)")
                return {"status": "spread_blocked", "spread": spread}

        order = open_position(exec_symbol, side, margin,
                              self.leverage, self.key)
        self.shadow.note_fill()
        pos = order.get("position") or {}
        pid = pos.get("id", "?")
        if micro:
            emit(f"    [execution] CHAOS MODE: Micro-position opened. "
                 f"Size: {margin / max(equity_usd, 1e-9) * 100:.1f}% "
                 f"({pid})")
        emit(f"    [execution] Position opened on MarginPad: "
             f"{pid} ({side.upper()}) margin ${margin} "
             f"lev={self.leverage}x @ {pos.get('entry_price', price)}")
        return {"status": "filled", "side": side, "id": pid,
                "order": order, "risk_mode":
                    "MICRO" if micro else "NORMAL"}

    @staticmethod
    def _spread_pct(symbol: str) -> float | None:
        """Bid/ask-спред в % от mid по ccxt. None — фид недоступен
        (не блокируем сделку по ошибке фида)."""
        ex = None
        try:
            import ccxt
            ex = ccxt.binance({"enableRateLimit": True})
            t = ex.fetch_ticker(
                f"{symbol}/USDT" if "/" not in symbol else symbol)
            bid, ask = t.get("bid"), t.get("ask")
            if bid and ask:
                mid = (float(ask) + float(bid)) / 2
                return (float(ask) - float(bid)) / mid * 100 if mid else None
        except Exception:                 # noqa: BLE001
            return None
        finally:
            if ex is not None:
                try:
                    ex.close()
                except Exception:         # noqa: BLE001 — best-effort cleanup
                    pass
        return None


def main() -> int:
    try:
        from tools.ansi_demo import slow_print, logo_lines, rule
        say = slow_print
        for ln in logo_lines():
            say(ln)
        say(rule("═"))
        say("  FUNDING ARB EXECUTOR — MarginPad (real-price sim)")
        say(rule("═"))
        emit = say
    except ImportError:
        emit = print
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-rate", type=float, default=FUNDING_THRESHOLD)
    ap.add_argument("--margin", type=float, default=100.0,
                    help="USDT на ногу")
    ap.add_argument("--leverage", type=int, default=3)
    ap.add_argument("--funding-rate", type=float, default=0.0,
                    help="внешняя ставка фандинга (доля); MarginPad её "
                         "не отдаёт — подаём из funding_arb фида")
    ap.add_argument("--close", metavar="POSITION_ID", default="",
                    help="закрыть позицию по id и выйти")
    args = ap.parse_args()

    ex = FundingExecutor(margin_usd=args.margin, leverage=args.leverage)
    emit(f"    mode={'MARGINPAD LIVE' if ex.live else 'DRY-RUN (no key)'}")

    if args.close and ex.key:
        r = close_position(args.close, ex.key)
        emit(f"    [funding] close {args.close}: {json.dumps(r)[:200]}")
        return 0

    res = ex.run_once(emit, min_rate=args.min_rate,
                      funding_rate=args.funding_rate)
    emit(f"    done → {res.get('status')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
