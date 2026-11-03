"""demo_news_sentiment.py — демонстрация ИИ-фильтра новостей.

  python -m tools.trading_ai2.swarm.demo_news_sentiment

Показывает весь путь целиком на реальных данных:
  1. заголовки — CoinDesk RSS (или CryptoPanic, если есть ключ);
  2. оценка фона — LLM (DeepSeek, если задан DEEPSEEK_API_KEY);
  3. вердикт ShadowRedteam — BUY при RSI>70 и оценке < -0.5 блокируется.

Вторым этапом идёт тот же сценарий на замоканном ответе DeepSeek —
ровно тот, который проверяет юнит-тест.

Ничего не торгует: paper-исполнение, биржа не трогается.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.trading_ai2.swarm.news_sentiment import (
    get_market_sentiment, fetch_headlines, reset_cache, MOCK_HEADLINES)
from tools.trading_ai2.swarm.shadow_redteam import (
    ShadowRedteam, OrderContext)
from tools.trading_ai2.swarm.fast_executor import FastExecutor
from tools.trading_ai2.swarm.arbiter import Consensus

try:
    from ansi_demo import stage, gold, cyan, green, magenta, red, dim, \
        rule, slow_print, logo_lines
except ImportError:
    from tools.ansi_demo import stage, gold, cyan, green, magenta, red, \
        dim, rule, slow_print, logo_lines

PRICE = 80_000.0
STOP = 78_000.0

MOCK_LLM_JSON = ('{"sentiment": -0.85, '
                 '"summary": "взломы и обвал — покупать нельзя"}')


def _log(say, tag: str, msg: str, color=dim) -> None:
    say(color(f"    {tag:<12} {msg}"))


class _FakeCompletions:
    """Ответ DeepSeek из юнит-теста — без сети."""

    def create(self, **kwargs):
        msg = SimpleNamespace(content=MOCK_LLM_JSON)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class _FakeOpenAI:
    def __init__(self, *args, **kwargs):
        self.chat = SimpleNamespace(completions=_FakeCompletions())


def _gate(say, score: float, mode: str, rsi: float) -> list[str]:
    """Вердикт тени на покупку: BUY + RSI>70 + score<-0.5 → блок."""
    blocks = ShadowRedteam().inspect(OrderContext(
        action="BUY", decision_price=PRICE, market_price=PRICE,
        stop_loss=STOP, rsi=rsi,
        sentiment_score=score, sentiment_mode=mode))
    if blocks:
        for b in blocks:
            _log(say, "[shadow]", f"Execution BLOCKED. {b}", red)
        _log(say, "[guard]", "ордер не создан — до биржи не дошло", red)
    else:
        _log(say, "[shadow]", "чисто — тень не сработала", green)
    return blocks


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="BTC")
    p.add_argument("--rsi", type=float, default=72.0)
    a = p.parse_args()

    say = slow_print
    for ln in logo_lines():
        say(ln)
    say(rule("═"))
    say(stage("  LLM NEWS SENTIMENT — Тень 6 «Эйфория»"))
    say(rule("═"))

    # ── 1. заголовки ─────────────────────────────────────────────
    headlines, source = fetch_headlines(a.symbol)
    _log(say, "[feed]", f"источник: {source}", cyan)
    for i, h in enumerate(headlines, 1):
        say(cyan(f"      {i}. {h[:96]}"))

    # ── 2. оценка LLM ────────────────────────────────────────────
    reset_cache()
    res = get_market_sentiment(a.symbol, headlines=headlines, use_cache=False)
    _log(say, "[llm]", f"оценка фона: {res.score:+.2f} ({res.label})",
         red if res.is_panic else green)
    _log(say, "[llm]", f"mode={res.mode} provider={res.provider} "
         f"model={res.model}", dim)
    _log(say, "[llm]", f"{res.summary}", dim)

    # ── 3. вердикт тени ──────────────────────────────────────────
    say("")
    say(rule())
    say(stage(f"  BUY @ {PRICE:.0f} · RSI {a.rsi:.1f} · stop {STOP:.0f}"))
    say(rule())

    _gate(say, res.score, res.mode, a.rsi)

    # тот же путь через весь конвейер consensus → shadow → биржа
    fex = FastExecutor(mode="paper", db_path=":memory:")
    con = Consensus("BUY", 0.85, {"BUY": 50}, 1.5, STOP, "demo")
    out = fex.execute(con, PRICE, rsi=a.rsi,
                      sentiment_score=res.score, sentiment_mode=res.mode)
    if out.get("status") == "shadow_blocked":
        _log(say, "[exec]", f"{out['status']}: {out['reasons'][0]}", red)
    else:
        _log(say, "[exec]", f"{out.get('status')}", gold)
    _log(say, "[exec]", f"позиция после вердикта: "
         f"{fex.ex.position() or 'пусто'}", dim)

    # ── 4. тот же сценарий на замоканном ответе DeepSeek ─────────
    say("")
    say(rule("═"))
    say(stage("  MOCKED DEEPSEEK — сценарий юнит-теста"))
    say(rule("═"))
    import openai
    with patch.object(openai, "OpenAI", _FakeOpenAI), \
            patch("tools.trading_ai2.swarm.news_sentiment._pick_models",
                  return_value=[]), \
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-mocked-deepseek"}):
        reset_cache()
        mres = get_market_sentiment(a.symbol, headlines=list(MOCK_HEADLINES),
                                    use_cache=False)
    _log(say, "[llm]", f"оценка фона: {mres.score:+.2f} ({mres.label})",
         red if mres.is_panic else green)
    _log(say, "[llm]", f"mode={mres.mode} provider={mres.provider} "
         f"model={mres.model}", dim)
    say("")
    say(rule())
    say(stage(f"  BUY @ {PRICE:.0f} · RSI {a.rsi:.1f} · stop {STOP:.0f}"))
    say(rule())
    _gate(say, mres.score, mres.mode, a.rsi)

    say(rule("═"))
    say(green("  NEWS SENTIMENT FILTER — exit 0"))
    say(rule("═"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
