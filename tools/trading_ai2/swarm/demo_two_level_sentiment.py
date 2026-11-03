"""demo_two_level_sentiment.py — доказательство двухуровневых новостей.

    python -m tools.trading_ai2.swarm.demo_two_level_sentiment

Реальные данные: живые заголовки (RSS/CryptoPanic), живая оценка LLM,
живые свечи Binance. Ничего не торгует — только чтение.

  1. уровень 1 — глобальный фон рынка (market_sentiment);
  2. уровень 2 — новости по активу (asset_sentiment);
  3. снапшот рынка по активу;
  4. голос роя: в reason агента видны оба сентимента;
  5. живой LLM-агент решает по двум сентиментам;
  6. матрица решений Analyst (плохо/плохо, плохо/хорошо, …);
  7. правила базы знаний про два сентимента.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.trading_ai2.market_data import MarketData            # noqa: E402
from tools.trading_ai2.strategy_agent import StrategyAgent       # noqa: E402
from tools.trading_ai2.swarm.agent_swarm import make_swarm      # noqa: E402
from tools.trading_ai2.swarm.knowledge import load_rules        # noqa: E402
from tools.trading_ai2.swarm.llm_factory import LLMFactory      # noqa: E402
from tools.trading_ai2.swarm.news_sentiment import (           # noqa: E402
    fetch_headlines, get_asset_sentiment, get_market_sentiment)


def head(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def vote_line(i: int, total: int, ag, vote) -> str:
    """Строка голоса в формате TUI (как в demo_trading_grand)."""
    ctx = ag.timeframe + (f"·{ag.asset}" if ag.asset else "")
    return (f"  [vote {i:02d}/{total}] {ag.name} ({ctx}) → "
            f"{vote.action:<4} conf {vote.confidence:.2f} w {vote.weight:.2f} · "
            f"{vote.reason[:160]}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="SOL/USDT")
    p.add_argument("--agents", type=int, default=13)
    p.add_argument("--no-llm", action="store_true",
                   help="без живого LLM-агента (только правила персон)")
    a = p.parse_args()

    base = a.symbol.split("/")[0].upper()

    # ---------------------------------------------- 1. глобальный фон
    head("1) УРОВЕНЬ 1 — ГЛОБАЛЬНЫЙ ФОН (market_sentiment)")
    g_head, g_src = fetch_headlines("")
    print(f"источник: {g_src} · заголовков: {len(g_head)}")
    for i, h in enumerate(g_head[:4], 1):
        print(f"  {i}. {h[:96]}")
    g = get_market_sentiment("", headlines=g_head, use_cache=False)
    print(f"оценка: {g.score:+.2f} ({g.label}) · режим {g.mode} · "
          f"{g.provider}/{g.model}")
    print(f"вывод: {g.summary}")

    # ------------------------------------------ 2. сентимент по активу
    head(f"2) УРОВЕНЬ 2 — НОВОСТИ ПО АКТИВУ (asset_sentiment · {base})")
    a_head, a_src = fetch_headlines(base)
    print(f"источник: {a_src} · заголовков: {len(a_head)}")
    for i, h in enumerate(a_head[:4], 1):
        print(f"  {i}. {h[:96]}")
    ast = get_asset_sentiment(base, headlines=a_head, use_cache=False)
    print(f"оценка: {ast.score:+.2f} ({ast.label}) · режим {ast.mode} · "
          f"{ast.provider}/{ast.model}")
    print(f"вывод: {ast.summary}")

    # ------------------------------------------------ 3. снапшот рынка
    head(f"3) СНАПШОТ РЫНКА {a.symbol} (реальные свечи Binance)")
    try:
        md = MarketData("binance", a.symbol)
        snap = md.snapshot_mtf(("1m", "15m"))
        print(f"цена {snap.get('price')} · RSI {snap.get('rsi')} · "
              f"MACD {snap.get('macd')} · ATR-рост "
              f"{snap.get('atr_growth_pct')}% · режим {snap.get('regime')}")
    except Exception as e:                                   # noqa: BLE001
        print(f"свечи недоступны: {e}")
        snap = {"symbol": a.symbol, "price": 0.0, "rsi": 50.0,
                "macd": 0.0, "ema9_above_ema21": False,
                "volume_ratio": 1.0, "atr_growth_pct": 0.0}
    snap = {**snap,
            "sentiment_score": g.score,
            "market_sentiment": g.score,
            "asset_sentiment": ast.score}

    # --------------------------------------- 4. голос роя (реальный код)
    head("4) ГОЛОС РОЯ — reason агента с двумя сентиментами")
    print(f"market_sentiment={g.score:+.2f} ({g.label}) · "
          f"asset_sentiment={ast.score:+.2f} ({ast.label})")
    swarm = make_swarm(a.agents, symbol=a.symbol)
    for ag in swarm:
        ag.refresh_prompt(asset=a.symbol)
    votes = []
    for i, ag in enumerate(swarm, 1):
        v = ag.decide(snap)
        votes.append(v)
        print(vote_line(i, len(swarm), ag, v))
    print()
    print("фрагмент промпта персоны (правило двух сентиментов):")
    rule = [ln for ln in swarm[0].system_prompt.split(". ")
            if "asset_sentiment" in ln]
    if rule:
        print("  " + rule[0].strip()[:300])

    # ------------------------------------------- 5. живой LLM-агент
    head("5) ЖИВОЙ LLM-АГЕНТ — решение по двум сентиментам")
    if a.no_llm:
        print("пропущено (--no-llm)")
    else:
        models = LLMFactory.role_models("generator")
        llm = StrategyAgent(model=models[0] if models else None,
                             role="generator")
        if not llm.live:
            print("нет живого ключа — LLM-агент пропущен")
        else:
            analyst = next((ag for ag in swarm
                            if ag.persona == "Analyst"), swarm[0])
            live_agent = replace(analyst, llm=llm)
            v = live_agent.decide(snap)
            print(f"модель: {llm.model} через {llm.base_url}")
            print(f"вход модели: market_sentiment={g.score:+.2f} · "
                  f"asset_sentiment={ast.score:+.2f} · "
                  f"цена {snap.get('price')} · RSI {snap.get('rsi')}")
            print(f"ответ модели: {v.action} conf {v.confidence:.2f} "
                  f"stop {v.stop_loss} · {v.reason[:220]}")

    # ---------------------------------------- 6. матрица решений Analyst
    head("6) МАТРИЦА РЕШЕНИЙ ANALYST (реальный код персоны)")
    analyst = next((ag for ag in swarm if ag.persona == "Analyst"), swarm[0])
    matrix = [(-0.65, -0.70), (-0.65, 0.42), (0.55, 0.48), (0.20, -0.55)]
    for mkt, ast_v in matrix:
        sc = {**snap, "market_sentiment": mkt, "asset_sentiment": ast_v}
        v = analyst.decide(sc)
        print(f"  фон {mkt:+.2f} / актив {ast_v:+.2f} → {v.action:<4} "
              f"conf {v.confidence:.2f} · {v.reason[:120]}")

    # --------------------------------------------- 7. правила базы знаний
    head("7) ПРАВИЛА БАЗЫ ЗНАНИЙ ПРО ДВА СЕНТИМЕНТА")
    rules = load_rules()
    for i, r in enumerate(rules, 1):
        if "сентимент" in r.lower():
            print(f"  #{i}: {r}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
