"""demo_three_minds.py — «Три Ума»: Генератор, Критик, Арбитр.

  python -m tools.trading_ai2.swarm.demo_three_minds          # горячий рынок
  python -m tools.trading_ai2.swarm.demo_three_minds --live   # реальный Binance
  python -m tools.trading_ai2.swarm.demo_three_minds --rsi 52

Диалог:
  ГЕНЕРАТОР (cyan)   предлагает сделку по 60 голосам роя;
  КРИТИК   (red/жёлтый) ищет слабые места и выносит возражения;
  АРБИТР    (green)  выносит вердикт: принято или ОТМЕНА.

На горячем рынке (RSI > 70) рой голосует за покупку, но Критик
возражает, а Арбитр отменяет сделку — до биржи не доходит.
Ничего не торгует: paper-исполнение, биржа не трогается.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.trading_ai2.swarm.agent_swarm import make_swarm
from tools.trading_ai2.swarm.generator import Generator
from tools.trading_ai2.swarm.critic import Critic
from tools.trading_ai2.swarm.arbiter import Arbiter
from tools.trading_ai2.swarm.fast_executor import FastExecutor
from tools.trading_ai2.swarm.news_sentiment import get_market_sentiment
from tools.trading_ai2.swarm.demo_swarm import three_minds

try:
    from ansi_demo import stage, gold, cyan, green, magenta, red, dim, \
        rule, slow_print, logo_lines
except ImportError:
    from tools.ansi_demo import stage, gold, cyan, green, magenta, red, \
        dim, rule, slow_print, logo_lines

HOT_SNAP = {
    "symbol": "BTC/USDT", "timeframe": "1m", "ts": "hot-market",
    "price": 80_000.0, "rsi": 70.5, "rsi_prev": 64.8,
    "macd": 600.0, "macd_signal": 120.0, "ema9": 79_800.0,
    "ema21": 78_900.0, "ema9_above_ema21": True, "source": "demo:hot",
}


def _log(say, tag: str, msg: str, color=dim) -> None:
    say(color(f"    {tag:<14} {msg}"))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="BTC/USDT")
    p.add_argument("--rsi", type=float, default=70.5)
    p.add_argument("--live", action="store_true",
                   help="реальный снимок Binance вместо горячего рынка")
    a = p.parse_args()

    say = slow_print
    for ln in logo_lines():
        say(ln)
    say(rule("═"))
    say(stage("  ТРИ УМА — Генератор → Критик → Арбитр"))
    say(rule("═"))

    if a.live:
        from tools.trading_ai2.market_data import MarketData
        snap = MarketData(symbol=a.symbol).snapshot("1m")
        say(cyan(f"    рынок: {snap['price']:.2f} RSI={snap['rsi']} "
                 f"(живой Binance)"))
    else:
        snap = dict(HOT_SNAP)
        snap["symbol"] = a.symbol
        snap["rsi"] = a.rsi
        say(cyan(f"    рынок: {snap['price']:.2f} RSI={snap['rsi']} "
                 f"(снимок горячего рынка)"))
    say(rule())

    swarm = make_swarm(60)
    votes = [(ag.decide(snap), ag.reputation_score) for ag in swarm]
    tally = {"BUY": 0, "SELL": 0, "HOLD": 0}
    for v, _ in votes:
        tally[v.action] += 1
    _log(say, "[swarm]", f"60 агентов: {tally['BUY']} BUY, "
         f"{tally['SELL']} SELL, {tally['HOLD']} HOLD", cyan)

    sentiment = get_market_sentiment(a.symbol.split("/")[0])
    _log(say, "[news]", f"sentiment {sentiment.score:+.2f} "
         f"({sentiment.label}, mode={sentiment.mode})", magenta)

    gen = Generator()
    critic = Critic()
    arb = Arbiter()
    say(rule())
    proposal, con = three_minds(
        say, gen, critic, arb, votes,
        rsi=snap.get("rsi"), sentiment_score=sentiment.score,
        sentiment_mode=sentiment.mode,
        tech_signal=("BUY" if snap["ema9_above_ema21"] else "SELL"))
    say(rule())

    # вердикт уходит в конвейер: если Арбитр отменил — ордера не будет
    fex = FastExecutor(mode="paper", db_path=":memory:")
    out = fex.execute(con, snap["price"], rsi=snap.get("rsi"),
                      sentiment_score=sentiment.score,
                      sentiment_mode=sentiment.mode,
                      tech_signal=("BUY" if snap["ema9_above_ema21"]
                                   else "SELL"))
    if con.cancelled:
        _log(say, "[arbiter]", "сделка отменена Критиком — до биржи "
             "не дошло", red)
    elif out.get("status") == "shadow_blocked":
        for reason in out["reasons"]:
            _log(say, "[shadow]", f"Execution BLOCKED. {reason}", red)
    elif out.get("status") == "filled":
        _log(say, "[execution]", f"Order filled at {out['price']:.2f}", gold)
    else:
        _log(say, "[execution]", f"{out.get('status')}: "
             f"{out.get('reason', '')}", dim)
    _log(say, "[position]", f"позиция: {fex.ex.position() or 'пусто'}", dim)

    say(rule("═"))
    say(green("  THREE MINDS — exit 0"))
    say(rule("═"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
