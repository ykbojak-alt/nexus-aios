"""main.py — оркестратор AI2 Live Trading Agent.

  python -m tools.trading_ai2.main                  # live-цикл paper/testnet
  python -m tools.trading_ai2.main --once           # один цикл
  python -m tools.trading_ai2.main --backtest 200   # на исторических свечах

Цикл: [data] snapshot → [llm] TradeDecision → [guard] review →
[execution] order. LLM никогда не трогает биржу — только решения через
Risk Manager. Стоп-лосс позиции проверяется на каждом тике ВНЕ LLM.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.trading_ai2.executor import Executor, START_USDT
from tools.trading_ai2.loop_guard import RiskManager
from tools.trading_ai2.market_data import MarketData
from tools.trading_ai2.strategy_agent import StrategyAgent, TradeDecision

try:
    from ansi_demo import stage, gold, cyan, green, magenta, link, red, \
        dim, rule, slow_print, logo_lines
except ImportError:
    from tools.ansi_demo import stage, gold, cyan, green, magenta, link, \
        red, dim, rule, slow_print, logo_lines

LOOP_SECONDS = 300


def _log(say, tag: str, msg: str, color=dim) -> None:
    say(color(f"    {tag:<12} {msg}"))


def run_cycle(feed_snap: dict, agent: StrategyAgent, guard: RiskManager,
              executor: Executor, say) -> dict:
    """Один цикл decide→guard→execute на уже собранном snapshot."""
    price = feed_snap["price"]
    _log(say, "[data]", f"{feed_snap['symbol']} {price:.2f} "
         f"RSI={feed_snap['rsi']} MACD={feed_snap['macd']:+.2f} "
         f"EMA9/21={'+' if feed_snap['ema9_above_ema21'] else '-'}", cyan)

    stopped = executor.stop_out(price)
    if stopped:
        _log(say, "[guard]", f"stop-loss сработал вне LLM → SELL "
             f"{stopped['qty']:.6f} @ {stopped['price']:.2f}", red)

    decision = agent.decide(feed_snap)
    _log(say, "[llm]", f"{decision.action} {decision.amount_percent}% "
         f"sl={decision.stop_loss} ({decision.mode}) — {decision.reason}",
         magenta)

    pf = executor.portfolio(price)
    review = guard.review(decision, pf)
    if not review.approved:
        _log(say, "[guard]", f"Trade blocked: {review.reason}", red)
        _log(say, "[execution]", "отклонено — до биржи не дошло", red)
        return {"decision": decision, "review": review, "exec": None}
    _log(say, "[guard]", "approved — позиция в лимитах", green)

    res = executor.execute(decision, price)
    if res.get("status") == "filled":
        _log(say, "[execution]",
             f"{res['side']} {res['qty']:.6f} @ {res['price']:.2f} "
             f"({res.get('order_id')})", gold)
        if res.get("pnl") is not None:
            _log(say, "[execution]",
                 f"PnL {res['pnl']:+.4f} USDT", gold)
    else:
        _log(say, "[execution]",
             f"{res.get('status')}: {res.get('reason', '')}", red)
    return {"decision": decision, "review": review, "exec": res}


def _equity_line(ex: Executor, price: float, say) -> None:
    st = ex.stats()
    eq = ex.equity(price)
    say(dim(f"    equity={eq:.2f} USDT (start {ex.start_usdt:.0f}) "
            f"closed={st['closed_trades']} wins={st['wins']} "
            f"realized={st['realized_pnl']:+.2f}"))


def run_live(say, symbol: str, mode: str, cycles: int | None) -> None:
    md = MarketData(symbol=symbol)
    agent = StrategyAgent()
    guard = RiskManager()
    ex = Executor(mode=mode, symbol=symbol)
    say(dim(f"    mode={mode} llm={'deepseek:' + agent.model if agent.live else 'stub'} "
            f"loop={LOOP_SECONDS}s symbol={symbol}"))
    n = 0
    while cycles is None or n < cycles:
        n += 1
        say("")
        say(stage(f"── цикл #{n} {time.strftime('%H:%M:%S')} ──"))
        snap = md.snapshot()
        run_cycle(snap, agent, guard, ex, say)
        _equity_line(ex, snap["price"], say)
        if cycles is None or n < cycles:
            time.sleep(LOOP_SECONDS)


def run_backtest(say, symbol: str, candles: int, timeframe: str) -> dict:
    """Исторический прогон: paper-исполнение на реальных свечах Binance."""
    md = MarketData(symbol=symbol)
    df = md.add_indicators(md.fetch_ohlcv(timeframe, limit=candles + 60))
    window = df.tail(candles).reset_index(drop=True)
    agent = StrategyAgent()
    guard = RiskManager()
    ex = Executor(mode="paper", symbol=symbol,
                  db_path=":memory:")
    say(dim(f"    backtest {symbol} {timeframe} ×{len(window)} "
            f"llm={'deepseek' if agent.live else 'stub'}"))
    eq_start = ex.cash
    for i in range(len(window)):
        row = window.iloc[i]
        snap = {
            "symbol": symbol, "timeframe": timeframe,
            "ts": str(row["ts"]), "price": float(row["close"]),
            "rsi": round(float(row["rsi"]), 2),
            "macd": round(float(row["macd"]), 4),
            "macd_signal": round(float(row["macd_signal"]), 4),
            "ema9": round(float(row["ema9"]), 2),
            "ema21": round(float(row["ema21"]), 2),
            "ema9_above_ema21": bool(row["ema9"] > row["ema21"]),
            "rsi_prev": round(float(window.iloc[i - 1]["rsi"]), 2)
            if i else round(float(row["rsi"]), 2),
            "source": f"backtest:{timeframe}",
        }
        say(rule())
        say(stage(f"  bar {i + 1}/{len(window)}  {row['ts']}"))
        run_cycle(snap, agent, guard, ex, say)
    say(rule("═"))
    st = ex.stats()
    eq_end = ex.equity(float(window.iloc[-1]["close"]))
    say(green("  BACKTEST RESULT"))
    say(f"    equity {eq_start:.2f} → {eq_end:.2f} USDT "
        f"({(eq_end - eq_start) / eq_start * 100:+.2f}%)")
    say(f"    trades={st['closed_trades']} wins={st['wins']} "
        f"realized={st['realized_pnl']:+.2f} "
        f"blocked={len(guard.blocked_log)}")
    say(rule("═"))
    return {"equity_end": eq_end, "stats": st,
            "blocked": guard.blocked_log}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="BTC/USDT")
    p.add_argument("--mode", choices=["paper", "testnet"], default="paper")
    p.add_argument("--once", action="store_true")
    p.add_argument("--cycles", type=int, default=None)
    p.add_argument("--backtest", type=int, metavar="N", default=0)
    p.add_argument("--demo-guard", action="store_true",
                   help="один цикл с подмешанным вредным решением LLM "
                        "(50%% депозита) — демо блокировки Risk Manager")
    p.add_argument("--timeframe", default="1h")
    a = p.parse_args()

    say = slow_print
    for ln in logo_lines():
        say(ln)
    say(rule("═"))
    say("  AI2 LIVE TRADING AGENT — LLM → RiskManager → Exchange")
    say(rule("═"))

    if a.demo_guard:
        md = MarketData(symbol=a.symbol)
        agent = StrategyAgent()
        guard = RiskManager()
        ex = Executor(mode="paper", db_path=":memory:")
        snap = md.snapshot()
        _log(say, "[data]", f"{snap['symbol']} {snap['price']:.2f} "
             f"RSI={snap['rsi']}", cyan)
        evil = TradeDecision.model_construct(
            action="BUY", amount_percent=50.0,
            reason="llm глюк: ALL IN", stop_loss=snap["price"] * 0.9)
        _log(say, "[llm]", f"{evil.action} {evil.amount_percent}% "
             f"sl={evil.stop_loss} (вредный ответ модели)", magenta)
        review = guard.review(evil, ex.portfolio(snap["price"]))
        _log(say, "[guard]",
             f"Trade blocked: {review.reason}", red)
        _log(say, "[execution]", "отклонено — до биржи не дошло", red)
    elif a.backtest:
        run_backtest(say, a.symbol, a.backtest, a.timeframe)
    else:
        run_live(say, a.symbol, a.mode, 1 if a.once else a.cycles)


if __name__ == "__main__":
    main()
