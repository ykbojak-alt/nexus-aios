"""demo_swarm.py — Swarm Trading Engine: живой цикл роя из 60 агентов.

  python -m tools.trading_ai2.swarm.demo_swarm              # live ×3 цикла
  python -m tools.trading_ai2.swarm.demo_swarm --cycles 5 --interval 2
  python -m tools.trading_ai2.swarm.demo_swarm --backtest 40 # история, paper
  python -m tools.trading_ai2.swarm.demo_swarm --no-epoch   # без сеттлмента

Публичные котировки — реальный api.binance.com через ccxt (ключей не
нужно). Ордера — paper или Binance Spot Testnet. Блокчейн трогается
только в конце: epoch-выплата лучшим через nexus-escrow.
Возвращает exit code 0 при штатном завершении — безопасен для
subprocess/TUI.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.trading_ai2.market_data import MarketData
from tools.trading_ai2.swarm.agent_swarm import make_swarm, SwarmAgent
from tools.trading_ai2.swarm.generator import Generator
from tools.trading_ai2.swarm.critic import Critic
from tools.trading_ai2.swarm.arbiter import Arbiter
from tools.trading_ai2.swarm.fast_executor import FastExecutor
from tools.trading_ai2.swarm.news_sentiment import get_market_sentiment
from tools.trading_ai2.swarm.epoch_rewarder import (
    EpochRewarder, load_reputation)

try:
    from ansi_demo import stage, gold, cyan, green, magenta, red, dim, \
        rule, slow_print, logo_lines
except ImportError:
    from tools.ansi_demo import stage, gold, cyan, green, magenta, red, \
        dim, rule, slow_print, logo_lines


def _log(say, tag: str, msg: str, color=dim) -> None:
    say(color(f"    {tag:<14} {msg}"))


def three_minds(say, gen: Generator, critic: Critic, arb: Arbiter,
                votes, *, rsi=None, sentiment_score=None,
                sentiment_mode="", tech_signal=None) -> tuple:
    """Диалог трёх умов: Генератор → Критик → Арбитр.

    Генератор предлагает, Критик возражает, Арбитр выносит вердикт.
    Цвета сущностей: cyan / red-жёлтый / green.
    """
    proposal = gen.propose(votes)
    say(cyan(f"    ГЕНЕРАТОР │ {gen.speech(proposal)}"))

    objections = critic.review(
        proposal, rsi=rsi, sentiment_score=sentiment_score,
        sentiment_mode=sentiment_mode, tech_signal=tech_signal)
    if not objections:
        say(gold("    КРИТИК   │ возражений нет — все довольны"))
    for ob in objections:
        say(red(f"    КРИТИК   │ {ob.text}")
            if ob.blocking else gold(f"    КРИТИК   │ {ob.text}"))

    con = arb.rule(proposal, objections)
    say(green(f"    АРБИТР    │ {con.speech()}"))
    return proposal, con


def run_cycle(say, n: int, snap: dict, swarm: list[SwarmAgent],
              gen: Generator, critic: Critic, arb: Arbiter,
              fex: FastExecutor, fresh_price: float | None = None) -> dict:
    """Один тик: snapshot → 60 голосов → три ума → guard → execution."""
    say(rule())
    say(stage(f"  цикл #{n}  {time.strftime('%H:%M:%S')}  "
              f"{snap['symbol']}"))
    _log(say, "[data]", f"{snap['price']:.2f} RSI={snap['rsi']} "
         f"MACD={snap['macd']:+.2f} "
         f"EMA9/21={'+' if snap['ema9_above_ema21'] else '-'}", cyan)

    votes = [(a.decide(snap), a.reputation_score) for a in swarm]
    t = {"BUY": 0, "SELL": 0, "HOLD": 0}
    for v, _ in votes:
        t[v.action] += 1
    _log(say, "[swarm]", f"{len(votes)} agents voted: "
         f"{t['BUY']} BUY, {t['SELL']} SELL, {t['HOLD']} HOLD.", cyan)

    # ── ИИ-аналитик новостей: оценка фона от LLM (кэш 5 мин) ─────
    sentiment = get_market_sentiment(snap["symbol"].split("/")[0])
    _log(say, "[news]", f"sentiment {sentiment.score:+.2f} "
         f"({sentiment.label}, mode={sentiment.mode}) — "
         f"{sentiment.summary}", magenta)

    # ── три ума: Генератор → Критик → Арбитр ──────────────────────
    say(rule())
    proposal, con = three_minds(
        say, gen, critic, arb, votes,
        rsi=snap.get("rsi"), sentiment_score=sentiment.score,
        sentiment_mode=sentiment.mode,
        tech_signal=("BUY" if snap["ema9_above_ema21"] else "SELL"))
    say(rule())

    res = fex.execute(
        con, snap["price"],
        market_price=fresh_price or snap["price"],
        rsi=snap.get("rsi"),
        sentiment_score=sentiment.score,
        sentiment_mode=sentiment.mode,
        tech_signal=("BUY" if snap["ema9_above_ema21"] else "SELL"))
    if res.get("status") == "shadow_blocked":
        for reason in res["reasons"]:
            _log(say, "[shadow]", f"Execution BLOCKED. {reason}", red)
    elif res.get("status") == "blocked":
        _log(say, "[guard]", f"Trade blocked: {res['reason']}", red)
    elif con.actionable:
        _log(say, "[guard]", "Risk checks passed.", green)

    if res.get("status") == "filled":
        _log(say, "[execution]",
             f"Order filled at {res['price']:.2f} "
             f"({res['side']} {res['qty']:.6f}, {res['order_id']})", gold)
        if res.get("pnl") is not None:
            _log(say, "[execution]", f"PnL {res['pnl']:+.4f} USDT", gold)
    elif res.get("status") == "skipped":
        _log(say, "[execution]",
             f"ордер не создан — {con.reason}", dim)
    else:
        _log(say, "[execution]",
             f"{res.get('status')}: {res.get('reason', '')}", red)
    return {"con": con, "res": res, "votes": votes}


def run_epoch(say, swarm: list[SwarmAgent], votes,
              entry_price: float, exit_price: float) -> dict:
    """Эпохальные расчёты: PnL → репутация → NEAR-выплата."""
    say(rule("═"))
    say(stage("  EPOCH SETTLEMENT"))
    rewarder = EpochRewarder()
    rewarder.attribute_pnl(swarm, [v for v, _ in votes],
                           entry_price, exit_price)
    rewarder.update_reputation(swarm)
    ranked = sorted(swarm, key=lambda a: a.pnl_usdt, reverse=True)
    top = [a for a in ranked[:15] if a.pnl_usdt > 0] or ranked[:15]
    _log(say, "[epoch]", "Updating on-chain reputation for "
         f"{len(swarm)} agents...", cyan)
    _log(say, "[epoch]", f"top-15: {', '.join(a.name for a in top[:5])}"
         f" ...", dim)
    res = rewarder.settle_onchain([a.name for a in top])
    tx = (res["txs"].get("verify_and_release")
          or res["txs"].get("stub", "?"))
    label = "" if res["mode"] == "onchain" else f" [{res['mode']}]"
    _log(say, "[epoch]", f"Distributing rewards for {len(top)} top "
         f"agents via NEAR Intents... TxHash: {tx}{label}", gold)
    if res["mode"] != "onchain":
        _log(say, "[epoch]", f"stub: {res.get('reason', '')[:90]}", dim)
    return res


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="BTC/USDT")
    p.add_argument("--cycles", type=int, default=3)
    p.add_argument("--interval", type=float, default=2.5)
    p.add_argument("--timeframe", default="1m")
    p.add_argument("--mode", choices=["paper", "testnet"], default="paper")
    p.add_argument("--backtest", type=int, metavar="N", default=0)
    p.add_argument("--no-epoch", action="store_true")
    a = p.parse_args()

    say = slow_print
    for ln in logo_lines():
        say(ln)
    say(rule("═"))
    say("  NEXUS SWARM TRADING — 60 agents → consensus → exchange")
    say(rule("═"))
    _log(say, "[swarm]", "Initializing 60 autonomous trading agents..."
         " connected to Binance Testnet.", cyan)

    md = MarketData(symbol=a.symbol)
    swarm = make_swarm(60, reputation=load_reputation())
    gen = Generator()
    critic = Critic()
    arb = Arbiter()
    fex = FastExecutor(mode=a.mode, symbol=a.symbol)
    _log(say, "[swarm]", f"feed=binance-public exec={a.mode} "
         f"timeframe={a.timeframe} guard=2%/5%/stop-loss", dim)

    entry_price = last_price = 0.0
    last_votes = []
    try:
        if a.backtest:
            df = md.add_indicators(md.fetch_ohlcv("1h",
                                                  limit=a.backtest + 60))
            window = df.tail(a.backtest).reset_index(drop=True)
            snaps = [{
                "symbol": a.symbol, "timeframe": "1h",
                "ts": str(r["ts"]), "price": float(r["close"]),
                "rsi": round(float(r["rsi"]), 2),
                "macd": round(float(r["macd"]), 4),
                "macd_signal": round(float(r["macd_signal"]), 4),
                "ema9": round(float(r["ema9"]), 2),
                "ema21": round(float(r["ema21"]), 2),
                "ema9_above_ema21": bool(r["ema9"] > r["ema21"]),
                "rsi_prev": round(float(window.iloc[i - 1]["rsi"]), 2)
                if i else round(float(r["rsi"]), 2),
                "source": "backtest:1h",
            } for i, r in window.iterrows()]
        else:
            snaps = None                      # live: тянем каждый тик
        for n in range(1, (len(snaps) if a.backtest else a.cycles) + 1):
            snap = snaps[n - 1] if a.backtest else md.snapshot(a.timeframe)
            entry_price = entry_price or snap["price"]
            last_price = snap["price"]
            stopped = fex.check_stop(last_price)
            if stopped:
                _log(say, "[guard]", "stop-loss вне роя → SELL "
                     f"{stopped['qty']:.6f} @ {stopped['price']:.2f}", red)
            fresh = None
            if not a.backtest:
                try:
                    fresh = float(md.ex.fetch_ticker(a.symbol)["last"])
                except Exception:
                    fresh = None
            out = run_cycle(say, n, snap, swarm, gen, critic, arb, fex,
                            fresh_price=fresh)
            last_votes = out["votes"]
            if not a.backtest and n < a.cycles:
                time.sleep(a.interval)
    except KeyboardInterrupt:
        say(red("\n    прервано — завершаю эпоху аккуратно"))
    except Exception as e:  # noqa: BLE001 — TUI увидит честную ошибку
        say(red(f"\n    [fatal] {e}"))
        return 1

    _log(say, "[swarm]", f"equity {fex.ex.equity(last_price):.2f} USDT "
         f"(start 10000) realized={fex.ex.stats()['realized_pnl']:+.2f}",
         dim)
    if not a.no_epoch and last_votes:
        run_epoch(say, swarm, last_votes, entry_price, last_price)
    say(rule("═"))
    say(green("  SWARM CYCLE COMPLETE — exit 0"))
    say(rule("═"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
