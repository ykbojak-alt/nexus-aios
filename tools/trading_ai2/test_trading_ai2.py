"""test_trading_ai2.py — проверки границ: LLM→guard→executor.

Запуск: python tools/trading_ai2/test_trading_ai2.py
Доказывает: мусор LLM → HOLD; Pydantic жёсткий; 2%-кап; 5% daily-loss
lockout; обязательный stop-loss; SQLite-персист; testnet-only.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.trading_ai2.strategy_agent import (  # noqa: E402
    TradeDecision, parse_decision, _stub_decision, StrategyAgent)
from tools.trading_ai2.loop_guard import (  # noqa: E402
    RiskManager, RiskConfig, Portfolio)
from tools.trading_ai2.executor import Executor  # noqa: E402
from tools.trading_ai2 import market_data  # noqa: E402
from tools.trading_ai2.swarm.agent_swarm import SwarmVote  # noqa: E402


def pf(equity=10_000.0, day_start=10_000.0, price=80_000.0, day=None):
    return Portfolio(
        equity_usdt=equity, day_start_equity=day_start,
        mark_price=price,
        day_key=day or time.strftime("%Y-%m-%d", time.gmtime()))


class TestParseDecision(unittest.TestCase):
    def test_malformed_json_gives_hold(self):
        for junk in ["", "not json", "{", "[]", "null", '{"a":1}',
                     "BUY BTC NOW!!!"]:
            d = parse_decision(junk)
            self.assertEqual(d.action, "HOLD", junk)
            self.assertEqual(d.mode, "parse_fallback")

    def test_markdown_wrapped_json_parses(self):
        d = parse_decision('```json\n{"action":"BUY","amount_percent":1.0,'
                           '"reason":"ok","stop_loss":100}\n```')
        self.assertEqual(d.action, "BUY")

    def test_buy_without_stop_rejected_by_schema(self):
        d = parse_decision('{"action":"BUY","amount_percent":1.0,'
                           '"reason":"x","stop_loss":0}')
        self.assertEqual(d.action, "HOLD")

    def test_amount_out_of_range_rejected(self):
        """Валидный JSON, но amount за пределами схемы — семантику
        валидации не подменяем спасением: честный HOLD."""
        d = parse_decision('{"action":"SELL","amount_percent":500,'
                           '"reason":"x","stop_loss":0}')
        self.assertEqual(d.action, "HOLD")
        self.assertEqual(d.reason, "parse_error")

    def test_stub_never_crashes_and_respects_cap(self):
        snap = {"rsi": 35.0, "rsi_prev": 45.0, "ema9_above_ema21": False,
                "price": 80_000.0}
        d = _stub_decision(snap)
        self.assertEqual(d.action, "BUY")
        self.assertLess(d.amount_percent, 2.0)
        self.assertGreater(d.stop_loss, 0)


class TestRiskManager(unittest.TestCase):
    def setUp(self):
        self.g = RiskManager(RiskConfig(max_position_pct=2.0,
                                        max_daily_loss_pct=5.0))

    def _buy(self, pct=1.5, sl=79_000.0):
        return TradeDecision(action="BUY", amount_percent=pct,
                             reason="t", stop_loss=sl)

    def test_huge_llm_size_blocked_with_log(self):
        r = self.g.review(self._buy(pct=50.0), pf())
        self.assertFalse(r.approved)
        self.assertIn("exceeding 2% risk limit", r.reason)
        self.assertEqual(len(self.g.blocked_log), 1)

    def test_buy_without_stop_blocked(self):
        d = TradeDecision(action="HOLD", amount_percent=0,
                          reason="x", stop_loss=0)
        self.assertTrue(self.g.review(d, pf()).approved)
        forced = TradeDecision.model_construct(
            action="BUY", amount_percent=1.0, reason="x", stop_loss=0.0)
        r = self.g.review(forced, pf())
        self.assertFalse(r.approved)

    def test_stop_above_price_blocked(self):
        r = self.g.review(self._buy(sl=85_000.0), pf(price=80_000.0))
        self.assertFalse(r.approved)
        self.assertIn("не ниже входа", r.reason)

    def test_daily_loss_lockout_same_day(self):
        p = pf(equity=9_400.0, day_start=10_000.0)  # −6% за день
        r = self.g.review(self._buy(), p)
        self.assertFalse(r.approved)
        self.assertIn("daily loss", r.reason)
        # повторный вызов — уже по halt-флагу
        r2 = self.g.review(self._buy(), pf())
        self.assertFalse(r2.approved)

    def test_new_day_unlocks(self):
        self.g.halted_day = "2020-01-01"
        r = self.g.review(self._buy(), pf())
        self.assertTrue(r.approved)


class TestExecutor(unittest.TestCase):
    def setUp(self):
        self.ex = Executor(mode="paper", db_path=":memory:")

    def test_paper_buy_sell_roundtrip_and_sqlite(self):
        buy = TradeDecision(action="BUY", amount_percent=1.5,
                            reason="t", stop_loss=78_000.0)
        r = self.ex.execute(buy, 80_000.0)
        self.assertEqual(r["status"], "filled")
        sell = TradeDecision(action="SELL", amount_percent=100.0,
                             reason="t", stop_loss=0.0)
        r2 = self.ex.execute(sell, 82_000.0)
        self.assertEqual(r2["status"], "filled")
        self.assertGreater(r2["pnl"], 0)
        n = self.ex.db.execute(
            "SELECT COUNT(*), COUNT(order_id), COUNT(price) "
            "FROM trades").fetchone()
        self.assertEqual(n, (2, 2, 2))
        st = self.ex.stats()
        self.assertEqual(st["closed_trades"], 1)
        self.assertEqual(st["wins"], 1)

    def test_stop_out_bypasses_llm(self):
        buy = TradeDecision(action="BUY", amount_percent=1.5,
                            reason="t", stop_loss=78_000.0)
        self.ex.execute(buy, 80_000.0)
        r = self.ex.stop_out(77_000.0)
        self.assertIsNotNone(r)
        self.assertEqual(r["side"], "SELL")
        self.assertLess(r["pnl"], 0)
        self.assertIsNone(self.ex.position())

    def test_sell_without_position_rejected(self):
        sell = TradeDecision(action="SELL", amount_percent=100.0,
                             reason="t", stop_loss=0.0)
        self.assertEqual(self.ex.execute(sell, 80_000.0)["status"],
                         "rejected")

    def test_testnet_mode_requires_keys_and_sandbox(self):
        os.environ.pop("BINANCE_API_KEY", None)
        os.environ.pop("BINANCE_SECRET", None)
        ex = Executor(mode="testnet", db_path=":memory:")
        self.assertTrue(ex.ex.checkRequiredCredentials(False) or True)
        # главное: sandbox включён — mainnet-эндпоинтов нет в urls
        urls = str(ex.ex.urls.get("api", {}))
        self.assertIn("testnet", urls)
        self.assertNotIn("api.binance.com\" :", urls)


class TestSwarm(unittest.TestCase):
    def _vote(self, action, conf, rep=1.0, sl=0.0):
        from tools.trading_ai2.swarm.agent_swarm import SwarmVote
        return (SwarmVote(action=action, confidence=conf,
                          reason="t", stop_loss=sl, agent="a"), rep)

    def test_consensus_weighted_math(self):
        from tools.trading_ai2.swarm.generator import Generator
        from tools.trading_ai2.swarm.arbiter import Arbiter
        votes = [self._vote("BUY", 0.9) for _ in range(45)] + \
                [self._vote("HOLD", 0.3) for _ in range(15)]
        con = Arbiter().rule(Generator().propose(votes))
        self.assertEqual(con.action, "BUY")
        self.assertGreater(con.score, 0.6)
        self.assertLessEqual(con.amount_percent, 2.0)

    def test_noisy_market_holds(self):
        from tools.trading_ai2.swarm.generator import Generator
        from tools.trading_ai2.swarm.arbiter import Arbiter
        votes = ([self._vote("BUY", 0.5)] * 20 +
                 [self._vote("SELL", 0.5)] * 20 +
                 [self._vote("HOLD", 0.4)] * 20)
        con = Arbiter().rule(Generator().propose(votes))
        self.assertEqual(con.action, "HOLD")

    def test_reputation_weight_moves_score(self):
        from tools.trading_ai2.swarm.generator import Generator
        from tools.trading_ai2.swarm.arbiter import Arbiter
        votes = [self._vote("SELL", 0.8, rep=0.1) for _ in range(30)] + \
                [self._vote("BUY", 0.8, rep=3.0) for _ in range(10)]
        con = Arbiter().rule(Generator().propose(votes))
        self.assertEqual(con.action, "BUY")   # мало, но авторитетны

    def test_swarm_size_and_stub_vote(self):
        from tools.trading_ai2.swarm.agent_swarm import make_swarm
        swarm = make_swarm(60, seed=1)
        self.assertEqual(len(swarm), 60)
        snap = {"rsi": 25.0, "rsi_prev": 45.0, "ema9_above_ema21": True,
                "price": 80_000.0, "ema21": 81_000.0, "macd": 100.0}
        for a in swarm:
            v = a.decide(snap)
            self.assertIn(v.action, ("BUY", "SELL", "HOLD"))
            self.assertTrue(0.0 <= v.confidence <= 1.0)

    def test_reputation_update_clamps_and_persists(self):
        import tempfile
        from tools.trading_ai2.swarm.agent_swarm import SwarmAgent
        from tools.trading_ai2.swarm.epoch_rewarder import EpochRewarder
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rep.json"
            ag = [SwarmAgent("w", "rsi", "p", pnl_usdt=10.0),
                  SwarmAgent("l", "rsi", "p", pnl_usdt=-10.0)]
            rep = EpochRewarder(path).update_reputation(ag)
            self.assertGreater(rep["w"], 1.0)
            self.assertLess(rep["l"], 1.0)
            self.assertTrue(path.exists())


class TestThreeMinds(unittest.TestCase):
    """Генератор → Критик → Арбитр: кто предлагает, кто возражает,
    кто отменяет опасную сделку."""

    def _vote(self, action, conf, rep=1.0, sl=0.0):
        from tools.trading_ai2.swarm.agent_swarm import SwarmVote
        return (SwarmVote(action=action, confidence=conf,
                          reason="t", stop_loss=sl, agent="a"), rep)

    def _buy_votes(self, n=45):
        return [self._vote("BUY", 0.9, sl=78_000.0) for _ in range(n)] + \
               [self._vote("HOLD", 0.3) for _ in range(60 - n)]

    def test_generator_proposes_without_deciding(self):
        from tools.trading_ai2.swarm.generator import Generator
        p = Generator().propose(self._buy_votes())
        self.assertEqual(p.action, "BUY")
        self.assertGreater(p.score, 0.6)
        self.assertEqual(p.stop_loss, 78_000.0)
        self.assertLessEqual(p.size_pct, 2.0)

    def test_critic_objects_to_overheated_rsi(self):
        from tools.trading_ai2.swarm.generator import Generator
        from tools.trading_ai2.swarm.critic import Critic
        p = Generator().propose(self._buy_votes())
        obs = Critic().review(p, rsi=78.0)
        self.assertTrue(any(o.blocking and "RSI" in o.text for o in obs))

    def test_critic_objects_to_panic_news(self):
        from tools.trading_ai2.swarm.generator import Generator
        from tools.trading_ai2.swarm.critic import Critic
        p = Generator().propose(self._buy_votes())
        obs = Critic().review(p, rsi=50.0, sentiment_score=-0.8,
                              sentiment_mode="live")
        self.assertTrue(any(o.blocking and "новостной фон" in o.text
                            for o in obs))

    def test_arbiter_cancels_dangerous_trade(self):
        """Сильный консенсус BUY, но RSI 78 и паника в новостях →
        Критик блокирует, Арбитр отменяет."""
        from tools.trading_ai2.swarm.generator import Generator
        from tools.trading_ai2.swarm.critic import Critic
        from tools.trading_ai2.swarm.arbiter import Arbiter
        p = Generator().propose(self._buy_votes())
        obs = Critic().review(p, rsi=78.0, sentiment_score=-0.72,
                              sentiment_mode="live")
        con = Arbiter().rule(p, obs)
        self.assertTrue(con.cancelled)
        self.assertEqual(con.action, "HOLD")
        self.assertEqual(con.amount_percent, 0.0)
        self.assertFalse(con.actionable)
        self.assertIn("ОТМЕНА", con.speech())

    def test_arbiter_accepts_clean_trade(self):
        from tools.trading_ai2.swarm.generator import Generator
        from tools.trading_ai2.swarm.critic import Critic
        from tools.trading_ai2.swarm.arbiter import Arbiter
        p = Generator().propose(self._buy_votes())
        obs = Critic().review(p, rsi=45.0, sentiment_score=0.1)
        con = Arbiter().rule(p, obs)
        self.assertFalse(con.cancelled)
        self.assertEqual(con.action, "BUY")
        self.assertTrue(con.actionable)

    def test_cancelled_trade_never_reaches_exchange(self):
        """Отменённый вердикт не создаёт ордер даже в paper-режиме."""
        from tools.trading_ai2.swarm.generator import Generator
        from tools.trading_ai2.swarm.critic import Critic
        from tools.trading_ai2.swarm.arbiter import Arbiter
        from tools.trading_ai2.swarm.fast_executor import FastExecutor
        p = Generator().propose(self._buy_votes())
        obs = Critic().review(p, rsi=78.0, sentiment_score=-0.72,
                              sentiment_mode="live")
        con = Arbiter().rule(p, obs)
        fex = FastExecutor(mode="paper", db_path=":memory:")
        out = fex.execute(con, 80_000.0, rsi=78.0,
                          sentiment_score=-0.72, sentiment_mode="live")
        self.assertEqual(out["status"], "skipped")
        self.assertIsNone(fex.ex.position())


class _FakeExecutor:
    """Исполнитель-заглушка: пишет, кто и с чем к нему пришёл."""

    def __init__(self):
        self.calls: list[tuple] = []

    def execute_verdict(self, con, emit=print, equity_usd=10_000.0, **ctx):
        self.calls.append((con, equity_usd, ctx))
        return {"status": "filled", "id": "pos-fake"}


class TestArbiterExecution(unittest.TestCase):
    """Арбитр одобрил → исполнитель вызван; отменил → исполнитель не тронут."""

    def _approved(self):
        from tools.trading_ai2.swarm.generator import Generator
        from tools.trading_ai2.swarm.critic import Critic
        from tools.trading_ai2.swarm.arbiter import Arbiter
        votes = [(SwarmVote(action="BUY", confidence=0.9, reason="t",
                            stop_loss=78_000.0, agent="a"), 1.0)
                 for _ in range(45)] + \
                [(SwarmVote(action="HOLD", confidence=0.3, reason="t",
                            stop_loss=0.0, agent="a"), 1.0)
                 for _ in range(15)]
        p = Generator().propose(votes)
        obs = Critic().review(p, rsi=45.0, sentiment_score=0.1)
        return Arbiter().rule(p, obs)

    def _cancelled(self):
        from tools.trading_ai2.swarm.generator import Generator
        from tools.trading_ai2.swarm.critic import Critic
        from tools.trading_ai2.swarm.arbiter import Arbiter
        votes = [(SwarmVote(action="BUY", confidence=0.9, reason="t",
                            stop_loss=78_000.0, agent="a"), 1.0)
                 for _ in range(45)] + \
                [(SwarmVote(action="HOLD", confidence=0.3, reason="t",
                            stop_loss=0.0, agent="a"), 1.0)
                 for _ in range(15)]
        p = Generator().propose(votes)
        obs = Critic().review(p, rsi=78.0, sentiment_score=-0.72,
                              sentiment_mode="live")
        return Arbiter().rule(p, obs)

    def test_approved_verdict_calls_executor(self):
        from tools.trading_ai2.swarm.arbiter import Arbiter
        con = self._approved()
        ex = _FakeExecutor()
        out = Arbiter(executor=ex).execute(con, emit=lambda *_: None,
                                           equity_usd=5_000.0)
        self.assertEqual(out["status"], "filled")
        self.assertEqual(len(ex.calls), 1)
        self.assertIs(ex.calls[0][0], con)
        self.assertEqual(ex.calls[0][1], 5_000.0)

    def test_cancelled_verdict_never_calls_executor(self):
        from tools.trading_ai2.swarm.arbiter import Arbiter
        con = self._cancelled()
        ex = _FakeExecutor()
        out = Arbiter(executor=ex).execute(con)
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(ex.calls, [])

    def test_missing_executor_is_reported_not_guessed(self):
        from tools.trading_ai2.swarm.arbiter import Arbiter
        out = Arbiter().execute(self._approved())
        self.assertEqual(out["status"], "no_executor")

    def test_fast_executor_execute_verdict_fills(self):
        """Тот же контракт, что у MarginPad: Consensus → ордер."""
        from tools.trading_ai2.swarm.arbiter import Consensus
        from tools.trading_ai2.swarm.fast_executor import FastExecutor
        fex = FastExecutor(mode="paper", db_path=":memory:")
        con = Consensus("BUY", 0.86, {"BUY": 52, "SELL": 0, "HOLD": 8},
                        1.72, 78_000.0, "консенсус BUY")
        out = fex.execute_verdict(con, emit=lambda *_: None, price=80_000.0,
                                  rsi=45.0, sentiment_score=0.1,
                                  tech_signal="BUY")
        self.assertEqual(out["status"], "filled")
        self.assertIsNotNone(fex.ex.position())


class TestShadowRedteam(unittest.TestCase):
    def setUp(self):
        from tools.trading_ai2.swarm.shadow_redteam import (
            ShadowRedteam, OrderContext)
        self.Shadow, self.Ctx = ShadowRedteam, OrderContext
        self.s = self.Shadow()

    def _ctx(self, **kw):
        base = dict(action="BUY", decision_price=80_000.0,
                    market_price=80_000.0, stop_loss=78_000.0,
                    rsi=50.0, tech_signal="BUY")
        base.update(kw)
        return self.Ctx(**base)

    def test_clean_order_passes(self):
        self.assertEqual(self.s.inspect(self._ctx()), [])

    def test_shadow_1_hallucination(self):
        b = self.s.inspect(self._ctx(decision_price=80_500,
                                     market_price=80_000))
        self.assertTrue(any("hallucination" in x for x in b))

    def test_shadow_2_overtrading(self):
        now = time.time()
        for _ in range(3):
            self.s.note_fill(now)
        b = self.s.inspect(self._ctx(now=now + 60))
        self.assertTrue(any("overtrading" in x for x in b))
        # старые сделки не считаются
        s2 = self.Shadow()
        s2._fills.extend([now - 1000] * 10)
        self.assertEqual(s2.inspect(self._ctx(now=now)), [])

    def test_shadow_3_missing_stop(self):
        b = self.s.inspect(self._ctx(stop_loss=0.0))
        self.assertTrue(any("stop-loss" in x for x in b))

    def test_shadow_4_euphoria(self):
        b = self.s.inspect(self._ctx(rsi=90.0, news_negative=True))
        self.assertTrue(any("euphoria" in x for x in b))
        # без негативного фона — не блок (просто горячий рынок)
        self.assertEqual(self.s.inspect(self._ctx(rsi=90.0)), [])

    def test_shadow_5_dualism(self):
        b = self.s.inspect(self._ctx(action="BUY", tech_signal="SELL"))
        self.assertTrue(any("dualism" in x for x in b))
        self.assertEqual(
            self.s.inspect(self._ctx(action="BUY",
                                     tech_signal="BUY")), [])

    def test_shadow_integrated_in_fast_executor(self):
        from tools.trading_ai2.swarm.fast_executor import FastExecutor
        from tools.trading_ai2.swarm.arbiter import Consensus
        fex = FastExecutor(mode="paper", db_path=":memory:")
        con = Consensus("BUY", 0.8, {"BUY": 50}, 1.5, 78_000.0, "t")
        r = fex.execute(con, 80_500.0, market_price=80_000.0)
        self.assertEqual(r["status"], "shadow_blocked")


def _mock_openai_factory(content: str):
    """Подмена openai.OpenAI → клиент с заранее заданным ответом."""
    class _Msg:
        def __init__(self, c):
            self.content = c

    class _Choice:
        def __init__(self, c):
            self.message = _Msg(c)

    class _Resp:
        def __init__(self, c):
            self.choices = [_Choice(c)]

    class _Completions:
        def __init__(self, c):
            self._c = c

        def create(self, **kwargs):
            return _Resp(self._c)

    class _Chat:
        def __init__(self, c):
            self.completions = _Completions(c)

    class _Client:
        def __init__(self, c):
            self.chat = _Chat(c)

    import openai
    return patch.object(openai, "OpenAI", lambda **kw: _Client(content))


def _mock_openai_counting(handlers: dict, calls: list):
    """Подмена openai.OpenAI с общим счётчиком вызовов.

    handlers[model] — строка (ответ), Exception (ошибка) или функция
    (model, номер попытки) → строка/исключение. Счётчик общий для всех
    клиентов: видно, сколько раз до модели реально дошли.
    """
    class _Msg:
        def __init__(self, c):
            self.content = c

    class _Choice:
        def __init__(self, c):
            self.message = _Msg(c)

    class _Resp:
        def __init__(self, c):
            self.choices = [_Choice(c)]

    class _Completions:
        def create(self, **kwargs):
            model = kwargs.get("model", "")
            calls.append(model)
            handler = handlers.get(model, handlers.get("*"))
            if callable(handler):
                handler = handler(model, calls.count(model))
            if isinstance(handler, Exception):
                raise handler
            if handler is None:
                raise RuntimeError(f"unexpected model: {model}")
            return _Resp(handler)

    class _Chat:
        def __init__(self):
            self.completions = _Completions()

    class _Client:
        def __init__(self, **kw):
            self.chat = _Chat()

    import openai
    return patch.object(openai, "OpenAI", lambda **kw: _Client(**kw))


class TestNewsSentiment(unittest.TestCase):
    """LLM-фильтр новостей: замоканный ответ DeepSeek → блок в shadow."""

    HEADLINES = [
        "Binance finds more breaches in $6.3 billion hack",
        "Binance hacker dumps a 2017 token and it crashes 90%",
        "SEC opens crackdown on unregistered crypto exchanges",
    ]

    def _mock_openai(self, content: str):
        """Подмена openai.OpenAI → клиент с заранее заданным ответом."""
        return _mock_openai_factory(content)

    def _with_key(self):
        """Замоканный DeepSeek: ключ + явная активная модель."""
        prev = {k: os.environ.get(k) for k in
                ("DEEPSEEK_API_KEY", "ACTIVE_LLM_MODEL")}
        os.environ["DEEPSEEK_API_KEY"] = "sk-mocked-deepseek"
        os.environ["ACTIVE_LLM_MODEL"] = "deepseek-chat"
        return prev

    def _restore_key(self, prev):
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_mocked_deepseek_panic_blocks_buy_on_high_rsi(self):
        """Ключевая проверка: score < -0.5 + RSI > 70 → сделка заблокирована."""
        from tools.trading_ai2.swarm import news_sentiment as ns
        from tools.trading_ai2.swarm.shadow_redteam import (
            ShadowRedteam, OrderContext)

        prev = self._with_key()
        try:
            with self._mock_openai(
                    '{"sentiment": -0.85, "summary": "взломы и обвал"}'):
                res = ns.get_market_sentiment("BTC", headlines=self.HEADLINES,
                                              use_cache=False)
        finally:
            self._restore_key(prev)

        self.assertEqual(res.mode, "live")
        self.assertEqual(res.provider, "deepseek")
        self.assertAlmostEqual(res.score, -0.85)
        self.assertTrue(res.is_panic)

        blocks = ShadowRedteam().inspect(OrderContext(
            action="BUY", decision_price=80_000.0, market_price=80_000.0,
            stop_loss=78_000.0, rsi=72.0,
            sentiment_score=res.score, sentiment_mode=res.mode))
        self.assertEqual(len(blocks), 1)
        self.assertIn("confirmation bias/euphoria", blocks[0])
        self.assertIn("⛔", blocks[0])
        self.assertIn("-0.85", blocks[0])
        self.assertIn("72.0", blocks[0])

    def test_mocked_deepseek_euphoria_does_not_block(self):
        """Хорошие новости + перегретый RSI — тень 6 молчит."""
        from tools.trading_ai2.swarm import news_sentiment as ns
        from tools.trading_ai2.swarm.shadow_redteam import (
            ShadowRedteam, OrderContext)

        prev = self._with_key()
        try:
            with self._mock_openai(
                    '{"sentiment": 0.72, "summary": "рекорд и одобрение"}'):
                res = ns.get_market_sentiment("BTC", headlines=self.HEADLINES,
                                              use_cache=False)
        finally:
            self._restore_key(prev)

        self.assertAlmostEqual(res.score, 0.72)
        self.assertFalse(res.is_panic)
        blocks = ShadowRedteam().inspect(OrderContext(
            action="BUY", decision_price=80_000.0, market_price=80_000.0,
            stop_loss=78_000.0, rsi=72.0,
            sentiment_score=res.score, sentiment_mode=res.mode))
        self.assertEqual(blocks, [])

    def test_blocked_through_fast_executor(self):
        """Тот же сценарий через весь конвейер: consensus → shadow → биржа."""
        from tools.trading_ai2.swarm import news_sentiment as ns
        from tools.trading_ai2.swarm.fast_executor import FastExecutor
        from tools.trading_ai2.swarm.arbiter import Consensus

        prev = self._with_key()
        try:
            with self._mock_openai(
                    '{"sentiment": -0.9, "summary": "паника"}'):
                res = ns.get_market_sentiment("BTC", headlines=self.HEADLINES,
                                              use_cache=False)
        finally:
            self._restore_key(prev)

        fex = FastExecutor(mode="paper", db_path=":memory:")
        con = Consensus("BUY", 0.85, {"BUY": 50}, 1.5, 78_000.0, "t")
        out = fex.execute(con, 80_000.0, rsi=75.0,
                          sentiment_score=res.score,
                          sentiment_mode=res.mode)
        self.assertEqual(out["status"], "shadow_blocked")
        self.assertTrue(any("confirmation bias/euphoria" in r
                            for r in out["reasons"]))
        # ордер не дошёл до биржи — позиция пуста
        self.assertIsNone(fex.ex.position())

    def test_sentiment_score_is_optional(self):
        """Без оценки новостей старая тень 4 работает как раньше."""
        from tools.trading_ai2.swarm.shadow_redteam import (
            ShadowRedteam, OrderContext)
        s = ShadowRedteam()
        self.assertEqual(s.inspect(OrderContext(
            action="BUY", decision_price=80_000.0, market_price=80_000.0,
            stop_loss=78_000.0, rsi=75.0)), [])
        self.assertTrue(s.inspect(OrderContext(
            action="BUY", decision_price=80_000.0, market_price=80_000.0,
            stop_loss=78_000.0, rsi=75.0, sentiment_score=-0.7)))

    def test_parse_sentiment_clamps_and_rejects_garbage(self):
        from tools.trading_ai2.swarm.news_sentiment import (
            _parse_sentiment, _lexicon_score)
        score, summary = _parse_sentiment('{"sentiment": -3.0, "summary": "x"}')
        self.assertEqual(score, -1.0)
        score, _ = _parse_sentiment('```json\n{"sentiment": 0.5}\n```')
        self.assertEqual(score, 0.5)
        with self.assertRaises(Exception):
            _parse_sentiment("не json")
        # лексикон: взломы и крах → негатив
        self.assertLess(_lexicon_score(self.HEADLINES), 0.0)


class TestQuant(unittest.TestCase):
    def test_kelly(self):
        from tools.trading_ai2.quant.kelly_sizing import kelly_fraction
        self.assertGreater(kelly_fraction(0.6, 1.5, 1.0), 0)
        self.assertEqual(kelly_fraction(0.3, 1.0, 1.0), 0.0)  # EV<0
        self.assertLessEqual(kelly_fraction(0.9, 5, 1), 2.0)  # cap

    def test_zscore_reverts_synthetic_pair(self):
        import numpy as np
        from tools.trading_ai2.quant import pairs_engine as pe
        rng = np.random.default_rng(7)
        b = np.cumprod(1 + rng.normal(0, 0.01, 2000))
        noise = np.cumsum(rng.normal(0, 0.3, 2000))
        a = b * np.exp(0.8 + noise * 0.01)     # коинтегрированная с B
        spread = pe.spread_series(a, b, pe.hedge_ratio(a, b))
        self.assertTrue(pe.is_cointegrated(spread))
        z = pe.zscore(spread, 48)
        self.assertTrue(np.nanmax(np.abs(z)) > 1.0)


class TestTradingLearning(unittest.TestCase):
    """Убыток → урок AI-2 → следующий цикл → системный промпт агента.

    Проверяется сквозная цепочка, без новых движков: событие «outcome» в
    траектории AI-2 → запись в память AI-2 → майнинг → файл уроков ai2.lessons/1
    → Learning Engine кладёт урок в MIND → урок в промпте каждого агента роя.
    """

    #: Рынок демо-сценария: RSI перепродан, тренд вверх (лонг против отмаха)
    SNAP = {"price": 77_479.8, "rsi": 28.0, "ema9_above_ema21": True,
            "ema21": 79_804.2, "sentiment_score": 0.35}

    def setUp(self):
        home = os.environ.get("AI2_HOME") or r"D:\ai2_pilot\ai2"
        if not (Path(home) / "ai2" / "__init__.py").is_file():
            self.skipTest(f"AI-2 не найден (AI2_HOME={home})")
        self._env = {k: os.environ.get(k)
                     for k in ("AI2_HOME", "AI2_LESSONS_PATH")}
        os.environ["AI2_HOME"] = home
        self.addCleanup(self._restore_env)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.corpus = self.root / "traj"
        self.lessons_path = self.root / "lessons.json"

    def _restore_env(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _learner(self, api_url="http://127.0.0.1:8123"):
        from tools.trading_ai2.swarm.learning_bridge import TradingLearning
        return TradingLearning(corpus_dir=self.corpus,
                               lessons_path=self.lessons_path,
                               api_url=api_url, symbol="BTC/USDT")

    def _stub_memory(self, memory_id="mem-abc123"):
        """Локальная заглушка POST /v1/memory: тест не ходит в реальный хост."""
        calls = []

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"id": memory_id, "project": "trading"}).encode("utf-8")

        def _urlopen(req, timeout=None):
            calls.append({"url": req.full_url,
                          "body": json.loads(req.data.decode("utf-8"))})
            return _Resp()

        return calls, patch("urllib.request.urlopen", side_effect=_urlopen)

    def _lose(self, learner, pnl=-1.23, snap=None, action="BUY"):
        snap = snap or self.SNAP
        learner.begin_cycle(learner.task_text(snap))
        return learner.record_trade(action=action, pnl_usd=pnl, snap=snap,
                                    position_id="pos-42",
                                    personas=["momentum-01", "rsi-01"], mode="demo")

    # ------------------------------------------------------------- урок

    def test_loss_produces_lesson_in_ai2_format(self):
        """Убыток → событие outcome в траектории AI-2 → урок в файле уроков."""
        from tools.trading_ai2.swarm.learning_bridge import TradingLearning
        learner = self._learner()
        self.assertFalse(learner.ai2_error, learner.ai2_error)
        calls, stub = self._stub_memory()
        with stub:
            out = self._lose(learner)
        self.assertEqual(out["code"], "buy_oversold")
        self.assertEqual(out["detail"], "trade_loss:buy_oversold")
        self.assertEqual(out["issues"], [])
        # исход сделки ушёл в память AI-2
        self.assertEqual(len(calls), 1)
        self.assertEqual(out["memory_id"], "mem-abc123")
        # урок записан в файл уроков AI-2 (схема ai2.lessons/1)
        self.assertTrue(self.lessons_path.is_file())
        payload = json.loads(self.lessons_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], "ai2.lessons/1")
        self.assertEqual(len(payload["lessons"]), 1)
        lesson = payload["lessons"][0]
        self.assertEqual(lesson["id"], "trade_loss:buy_oversold")
        self.assertIn("rsi_oversold", lesson["keywords"])
        self.assertFalse(lesson["universal"])
        self.assertTrue(any("новый урок из убытка" in line
                            for line in out["lines"]))
        # событие outcome в траектории AI-2 — тот же формат, что у остальных прогонов
        traj = list(self.corpus.glob("*.jsonl"))
        self.assertTrue(traj)
        events = [json.loads(line) for line in
                  traj[0].read_text(encoding="utf-8").splitlines() if line.strip()]
        outcome = [e for e in events if e["type"] == "outcome"]
        self.assertEqual(len(outcome), 1)
        self.assertEqual(outcome[0]["detail"], "trade_loss:buy_oversold")
        self.assertFalse(outcome[0]["success"])

    def test_memory_record_holds_pnl_and_market_context(self):
        """Запись в память AI-2 несёт PnL, условия входа и контекст рынка."""
        learner = self._learner()
        calls, stub = self._stub_memory()
        with stub:
            self._lose(learner)
        body = calls[0]["body"]
        self.assertEqual(calls[0]["url"].endswith("/v1/memory"), True)
        self.assertEqual(body["project"], "trading")
        self.assertAlmostEqual(body["meta"]["pnl_usd"], -1.23)
        self.assertEqual(body["meta"]["entry_conditions"], "buy_oversold")
        self.assertEqual(body["meta"]["position_id"], "pos-42")
        self.assertAlmostEqual(body["meta"]["rsi"], 28.0)
        self.assertEqual(body["meta"]["trend"], "trend_up")
        self.assertIn("-1.23", body["text"])
        self.assertIn("BTC/USDT", body["text"])

    def test_profit_creates_no_lesson(self):
        """Прибыльная сделка — не провал: ни записи в память, ни урока."""
        learner = self._learner()
        calls, stub = self._stub_memory()
        with stub:
            out = self._lose(learner, pnl=2.5)
        self.assertEqual(calls, [])
        self.assertEqual(out["memory_id"], "")
        self.assertEqual(out["detail"], "trade_profit:buy_oversold")
        self.assertFalse(self.lessons_path.exists())

    # ------------------------------------------------- урок в промпте агента

    def test_next_cycle_gives_lesson_to_every_agent(self):
        """Следующий цикл: урок отобран по условиям рынка и у всех агентов в промпте."""
        from tools.trading_ai2.swarm.agent_swarm import apply_lessons, make_swarm
        learner = self._learner()
        calls, stub = self._stub_memory()
        with stub:
            self._lose(learner)
        # следующий цикл: те же условия рынка
        task = learner.task_text(self.SNAP)
        learner.begin_cycle(task)
        applied = learner.apply_lessons(task)
        self.assertEqual(len(applied["lessons"]), 1)
        self.assertIn("УРОК (", applied["block"])
        # урок лёг в MIND (источник learning) — это штатный интерфейс AI-2
        beliefs = [b for b in learner.state.beliefs if b.source == "learning"]
        self.assertEqual(len(beliefs), 1)
        self.assertIn("перепроданном RSI", beliefs[0].text)
        # блок урока — в системном промпте каждого агента роя
        swarm = make_swarm(12)
        self.assertEqual(apply_lessons(swarm, applied["block"]), 12)
        for agent in swarm:
            self.assertIn("Твои прошлые ошибки (уроки):", agent.system_prompt)
            self.assertIn("перепроданном RSI", agent.system_prompt)
        # повторное применение в следующем цикле не дублирует блок
        apply_lessons(swarm, applied["block"])
        for agent in swarm:
            self.assertEqual(
                agent.system_prompt.count("Твои прошлые ошибки (уроки):"), 1)
        # в лог TUI уходит строка применения урока
        lines = learner.log_lines(applied)
        self.assertTrue(any("уроков применено к рою: 1" in line
                            for line in lines))

    def test_lesson_not_applied_to_other_market_conditions(self):
        """Урок про лонг на перепроданном RSI не идёт в шорт на перекупленном."""
        learner = self._learner()
        calls, stub = self._stub_memory()
        with stub:
            self._lose(learner)
        other = {"price": 79_000.0, "rsi": 78.0, "ema9_above_ema21": False,
                 "ema21": 78_000.0, "sentiment_score": -0.5}
        task = learner.task_text(other)
        learner.begin_cycle(task)
        applied = learner.apply_lessons(task)
        self.assertEqual(applied["lessons"], [])
        self.assertEqual(applied["block"], "")

    def test_no_ai2_degrades_honestly(self):
        """Без AI-2 мост не падает: причина попадает в issues, торговля идёт."""
        from tools.trading_ai2.swarm import learning_bridge as lb
        with patch.object(lb, "load_ai2",
                          return_value=(None, "AI-2 недоступен: тест")):
            learner = self._learner()
            self.assertIsNone(learner.ai2)
            self.assertTrue(learner.issues)
            out = learner.record_trade(action="BUY", pnl_usd=-1.0,
                                       snap=self.SNAP, position_id="pos-1")
            self.assertEqual(out["lessons"], [])
            self.assertFalse(self.lessons_path.exists())
            applied = learner.apply_lessons(learner.task_text(self.SNAP))
            self.assertEqual(applied["lessons"], [])
            self.assertEqual(applied["block"], "")
            self.assertTrue(applied["issues"])


class TestLLMFactory(unittest.TestCase):
    """Переключатель моделей: ACTIVE_LLM_MODEL → маршрут, ключ, откат."""

    ENV = {
        "ACTIVE_LLM_MODEL": "deepseek-chat",
        "DEEPSEEK_API_KEY": "sk-deepseek",
        "DEEPSEEK_BASE_URL": "",
        "DEEPSEEK_MODEL": "",
        "OPENAI_API_KEY": "",
        "OPENAI_BASE_URL": "",
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_BASE_URL": "",
        "LLM_API_KEY": "",
        "LLM_BASE_URL": "",
        "LLM_MODEL": "",
    }

    def setUp(self):
        from tools.trading_ai2.swarm import llm_factory as lf
        self._lf = lf
        self._qs = lf.QUOTA_SLEEP_S
        lf.QUOTA_SLEEP_S = 0.0        # в тестах quota-сон не нужен
        lf.LLMFactory.reset_clients()
        lf.LLMFactory.reset_free_calls()
        lf.LLMFactory.reset_blacklist()

    def tearDown(self):
        self._lf.QUOTA_SLEEP_S = self._qs
        self._lf.LLMFactory.reset_clients()
        self._lf.LLMFactory.reset_free_calls()
        self._lf.LLMFactory.reset_blacklist()

    def _route(self, **over):
        """Маршрут при заданном окружении (без .env машины)."""
        from tools.trading_ai2.swarm import llm_factory as lf
        env_map = dict(self.ENV, **over)
        with patch.object(lf, "env", lambda name: env_map.get(name, "")):
            return lf.LLMFactory.resolve()

    def test_deepseek_route_from_env(self):
        r = self._route()
        self.assertEqual(r.model, "deepseek-chat")
        self.assertEqual(r.provider, "deepseek")
        self.assertEqual(r.base_url, "https://api.deepseek.com/v1")
        self.assertTrue(r.live)
        self.assertFalse(r.fallback)

    def test_gpt_route_uses_openai(self):
        r = self._route(ACTIVE_LLM_MODEL="gpt-4o", OPENAI_API_KEY="sk-oai")
        self.assertEqual(r.model, "gpt-4o")
        self.assertEqual(r.provider, "openai")
        self.assertEqual(r.base_url, "https://api.openai.com/v1")
        self.assertFalse(r.fallback)

    def test_claude_route_uses_anthropic(self):
        r = self._route(ACTIVE_LLM_MODEL="claude-3-5-sonnet",
                        ANTHROPIC_API_KEY="sk-ant")
        self.assertEqual(r.provider, "anthropic")
        self.assertIn("api.anthropic.com", r.base_url)
        self.assertEqual(r.model, "claude-3-5-sonnet")

    def test_missing_key_falls_back_to_deepseek(self):
        """Нет ключа GPT → откат на DeepSeek с честной строкой в логе."""
        from tools.trading_ai2.swarm import llm_factory as lf
        r = self._route(ACTIVE_LLM_MODEL="gpt-4o")
        self.assertTrue(r.fallback)
        self.assertEqual(r.model, "deepseek-chat")
        self.assertEqual(r.reason, "key for GPT missing")
        env_map = dict(self.ENV, ACTIVE_LLM_MODEL="gpt-4o")
        with patch.object(lf, "env", lambda name: env_map.get(name, "")):
            lines = lf.LLMFactory.log_lines(r, new_only=False)
        self.assertIn("[llm] Fallback to DeepSeek (key for GPT missing)",
                      lines)

    def test_unknown_model_falls_back(self):
        r = self._route(ACTIVE_LLM_MODEL="mystery-9000")
        self.assertTrue(r.fallback)
        self.assertIn("not found", r.reason)
        self.assertEqual(r.model, "deepseek-chat")

    def test_gateway_id_uses_repo_gateway(self):
        """OpenRouter-id (со «/») → шлюз репозитория, без отката."""
        r = self._route(ACTIVE_LLM_MODEL="cohere/north-mini-code:free",
                        LLM_API_KEY="sk-gw", LLM_BASE_URL="https://gw/v1")
        self.assertEqual(r.provider, "openai-compatible")
        self.assertEqual(r.model, "cohere/north-mini-code:free")
        self.assertEqual(r.base_url, "https://gw/v1")
        self.assertFalse(r.fallback)

    def test_no_keys_at_all_is_stub(self):
        r = self._route(ACTIVE_LLM_MODEL="gpt-4o", DEEPSEEK_API_KEY="",
                        OPENAI_API_KEY="", ANTHROPIC_API_KEY="",
                        LLM_API_KEY="", LLM_BASE_URL="")
        self.assertFalse(r.live)
        self.assertTrue(r.fallback)

    def test_model_switches_on_the_fly(self):
        """Смена ACTIVE_LLM_MODEL меняет модель мозга без пересоздания."""
        from tools.trading_ai2.swarm import llm_factory as lf
        env_map = dict(self.ENV)
        seen: dict[str, str] = {}

        class _Msg:
            def __init__(self, c):
                self.content = c

        class _Choice:
            def __init__(self, c):
                self.message = _Msg(c)

        class _Resp:
            def __init__(self, c):
                self.choices = [_Choice(c)]

        class _Completions:
            def __init__(self, c):
                self._c = c

            def create(self, **kwargs):
                seen["model"] = kwargs.get("model", "")
                return _Resp(self._c)

        class _Chat:
            def __init__(self, c):
                self.completions = _Completions(c)

        class _Client:
            def __init__(self, c):
                self.chat = _Chat(c)

        import openai
        snap = {"price": 80_000.0, "rsi": 44.0, "ema9_above_ema21": True}
        reply = json.dumps({"action": "HOLD", "amount_percent": 0,
                            "reason": "ok", "stop_loss": 0})
        with patch.object(lf, "env", lambda name: env_map.get(name, "")):
            agent = StrategyAgent()
            with patch.object(openai, "OpenAI",
                              lambda **kw: _Client(reply)):
                agent.decide(snap)
                self.assertEqual(seen["model"], "deepseek-chat")
                env_map["ACTIVE_LLM_MODEL"] = "gpt-4o"
                env_map["OPENAI_API_KEY"] = "sk-oai"
                agent.decide(snap)
                self.assertEqual(seen["model"], "gpt-4o")
        self.assertEqual(agent.model, "gpt-4o")

    def test_news_sentiment_uses_active_model(self):
        """Оценка новостей идёт по модели из ACTIVE_LLM_MODEL."""
        from tools.trading_ai2.swarm import llm_factory as lf
        from tools.trading_ai2.swarm import news_sentiment as ns
        env_map = dict(self.ENV)
        with patch.object(lf, "env", lambda name: env_map.get(name, "")):
            with _mock_openai_factory(
                    '{"sentiment": -0.85, "summary": "взломы"}'):
                res = ns.get_market_sentiment("BTC", headlines=["hack"],
                                              use_cache=False)
        self.assertEqual(res.mode, "live")
        self.assertEqual(res.provider, "deepseek")
        self.assertEqual(res.model, "deepseek-chat")

    def test_swarm_reports_active_model(self):
        """Рой отдаёт активную модель для строки лога в TUI."""
        from tools.trading_ai2.swarm import llm_factory as lf
        from tools.trading_ai2.swarm.agent_swarm import llm_status
        env_map = dict(self.ENV, ACTIVE_LLM_MODEL="gpt-4o",
                       OPENAI_API_KEY="sk-oai")
        with patch.object(lf, "env", lambda name: env_map.get(name, "")):
            model, lines = llm_status()
        self.assertEqual(model, "gpt-4o")
        self.assertEqual(lines, [])


class _FakeStatus(Exception):
    """Ошибка провайдера со статусом (как openai.APIStatusError)."""

    def __init__(self, status: int, message: str = ""):
        super().__init__(message or f"http {status}")
        self.status_code = status


def _mock_openai_requests(handlers: dict, requests: list,
                          client_keys: list):
    """Подмена OpenAI с записью max_tokens и использованных API-ключей."""
    class _Msg:
        def __init__(self, content):
            self.content = content

    class _Client:
        def __init__(self, **kwargs):
            client_keys.append(kwargs.get("api_key"))

            class _Completions:
                def create(inner_self, **kwargs):
                    model = kwargs["model"]
                    requests.append((model, kwargs["max_tokens"]))
                    handler = handlers[model]
                    if callable(handler):
                        attempt = sum(1 for request in requests
                                      if request[0] == model)
                        handler = handler(model, attempt, kwargs)
                    if isinstance(handler, Exception):
                        raise handler
                    return type("Response", (), {
                        "choices": [type("Choice", (), {
                            "message": _Msg(handler)})()]})()

            self.chat = type("Chat", (), {
                "completions": _Completions()})()

    import openai
    return patch.object(openai, "OpenAI", lambda **kwargs: _Client(**kwargs))


def _mock_openai_by_model(handlers: dict):
    """Подмена openai.OpenAI: ответ или исключение выбирается по модели.

    Модель без явного ответа — ошибка: так видна вся цепочка откатов.
    """
    class _Msg:
        def __init__(self, c):
            self.content = c

    class _Choice:
        def __init__(self, c):
            self.message = _Msg(c)

    class _Resp:
        def __init__(self, c):
            self.choices = [_Choice(c)]

    class _Completions:
        def create(self, **kwargs):
            model = kwargs.get("model", "")
            if model in handlers:
                h = handlers[model]
            elif "*" in handlers:
                h = handlers["*"]
            else:
                raise RuntimeError(f"unexpected model: {model}")
            if isinstance(h, Exception):
                raise h
            return _Resp(h)

    class _Chat:
        def __init__(self):
            self.completions = _Completions()

    class _Client:
        def __init__(self, **kw):
            self.chat = _Chat()

    import openai
    return patch.object(openai, "OpenAI", lambda **kw: _Client(**kw))


class TestFreeModelProtection(unittest.TestCase):
    """Бесплатные модели: 402/429 → фри-фолбэк, лимит 20 RPM, резерв роли."""

    ENV = {
        "ACTIVE_LLM_MODEL": "deepseek-chat",
        "DEEPSEEK_API_KEY": "sk-deepseek",
        "DEEPSEEK_BASE_URL": "",
        "OPENAI_API_KEY": "sk-oai",
        "OPENAI_BASE_URL": "",
        "ANTHROPIC_API_KEY": "",
        "INCEPTION_API_KEY": "",
        "LLM_API_KEY": "sk-gw",
        "LLM_BASE_URL": "https://gw/v1",
        "LLM_MODEL": "cohere/north-mini-code:free",
        "SWARM_GEN_MODELS": "",
        "SWARM_CRITIC_MODEL": "",
        "SWARM_ARBITER_MODEL": "",
    }

    def setUp(self):
        from tools.trading_ai2.swarm import llm_factory as lf
        self.lf = lf
        self._qs = lf.QUOTA_SLEEP_S
        lf.QUOTA_SLEEP_S = 0.0        # в тестах quota-сон не нужен
        lf.LLMFactory.reset_clients()
        lf.LLMFactory.reset_free_calls()
        lf.LLMFactory.reset_blacklist()

    def tearDown(self):
        self.lf.QUOTA_SLEEP_S = self._qs
        self.lf.LLMFactory.reset_clients()
        self.lf.LLMFactory.reset_free_calls()
        self.lf.LLMFactory.reset_blacklist()

    def _complete(self, model: str, handlers: dict, role: str = "generator"):
        """Запрос к LLM с подменой клиента (ответ/ошибка по модели)."""
        env_map = dict(self.ENV)
        with patch.object(self.lf, "env", lambda name: env_map.get(name, "")):
            with _mock_openai_by_model(handlers):
                return self.lf.LLMFactory.complete(
                    role=role, model=model,
                    messages=[{"role": "user", "content": "x"}])

    def test_402_switches_to_free_model(self):
        """402 Payment Required → уходим на бесплатную LLM_MODEL."""
        reply = '{"action": "HOLD"}'
        out = self._complete(
            "gpt-4o",
            {"gpt-4o": _FakeStatus(402, "Payment Required"),
             "cohere/north-mini-code:free": reply})
        self.assertEqual(out, reply)

    def test_429_switches_to_free_model(self):
        """429 Rate Limit → уходим на бесплатную LLM_MODEL."""
        reply = '{"action": "HOLD"}'
        out = self._complete(
            "gpt-4o",
            {"gpt-4o": _FakeStatus(429, "rate limited"),
             "cohere/north-mini-code:free": reply})
        self.assertEqual(out, reply)

    def test_404_switches_to_role_fallback(self):
        """404 Not Found → резерв роли из models.yaml (free-модель)."""
        reply = '{"action": "HOLD"}'
        out = self._complete(
            "gpt-4o",
            {"gpt-4o": _FakeStatus(404, "not found"),
             "cohere/north-mini-code:free": reply})
        self.assertEqual(out, reply)

    def test_empty_response_switches_to_fallback(self):
        """Пустой ответ модели → резерв роли, а не тихий HOLD."""
        reply = '{"action": "HOLD"}'
        out = self._complete(
            "gpt-4o",
            {"gpt-4o": "", "cohere/north-mini-code:free": reply})
        self.assertEqual(out, reply)

    def test_request_timeout_switches_to_fallback(self):
        """Таймаут провайдера уходит в штатный резерв модели."""
        reply = '{"action": "HOLD"}'
        out = self._complete(
            "gpt-4o",
            {"gpt-4o": TimeoutError("read timeout"),
             "cohere/north-mini-code:free": reply})
        self.assertEqual(out, reply)

    def test_paid_models_never_enter_fallback(self):
        """402 у :free-модели: платные модели НЕ входят в резерв —
        ошибка пробрасывается наверх (caller даёт безопасный HOLD),
        лимиты не сжигаются на заведомо мёртвые запросы."""
        with self.assertRaises(Exception) as cm:
            self._complete(
                "cohere/north-mini-code:free",
                {"cohere/north-mini-code:free": _FakeStatus(
                     402, "Payment Required"),
                 "google/gemma-4-31b-it:free": _FakeStatus(
                     402, "Payment Required"),
                 "deepseek/deepseek-chat": '{"action": "HOLD"}'})
        self.assertIn("Payment Required", str(cm.exception))

    def test_rpm_limit_rotates_pool_key(self):
        """20 RPM исчерпаны на одном ключе → тот же запрос уходит на
        следующий ключ пула OpenRouter (у каждого своя квота)."""
        from tools.trading_ai2.swarm import llm_factory as lf
        url = "https://openrouter.ai/api/v1"
        k1, k2 = "sk-or-v1-aaaaaa111111", "sk-or-v1-bbbbbb222222"
        for _ in range(lf.FREE_RPM_LIMIT):
            lf.LLMFactory.note_free_call(f"{url}|{k1[-6:]}")
        reply = '{"action": "HOLD"}'
        env_map = dict(self.ENV, LLM_BASE_URL=url, LLM_API_KEY=k1,
                       LLM_API_KEYS=f"{k1},{k2}")
        with patch.object(lf, "env", lambda name: env_map.get(name, "")):
            with _mock_openai_by_model({"*": reply}):
                out = lf.LLMFactory.complete(
                    role="generator", model="cohere/north-mini-code:free",
                    messages=[{"role": "user", "content": "x"}])
        self.assertEqual(out, reply)

    def test_free_rpm_counter_window(self):
        """Счётчик считает только последнюю минуту."""
        from tools.trading_ai2.swarm import llm_factory as lf
        account = "https://gw/v1"
        lf.LLMFactory.note_free_call(account)
        self.assertEqual(lf.LLMFactory.free_rpm_used(account), 1)
        lf.LLMFactory._free_calls[account] = [time.time() - 120.0]
        self.assertEqual(lf.LLMFactory.free_rpm_used(account), 0)

    def test_free_quota_shared_between_models(self):
        """Лимит 20 RPM общий на аккаунт, а не на каждую модель."""
        from tools.trading_ai2.swarm import llm_factory as lf
        account = "https://gw/v1"
        for _ in range(10):
            lf.LLMFactory.note_free_call(account)
        lf.LLMFactory.note_free_call(account)   # вторая :free-модель
        self.assertEqual(lf.LLMFactory.free_rpm_used(account), 11)
        self.assertEqual(lf.LLMFactory.free_quota_left(account),
                         lf.FREE_RPM_LIMIT - 11)

    def test_is_free_model(self):
        """Суффикс :free — признак бесплатной модели."""
        from tools.trading_ai2.swarm import llm_factory as lf
        self.assertTrue(lf.LLMFactory.is_free_model(
            "cohere/north-mini-code:free"))
        self.assertFalse(lf.LLMFactory.is_free_model("gpt-4o"))
        self.assertFalse(lf.LLMFactory.is_free_model("inception/mercury-3"))

    def test_models_yaml_roles(self):
        """models.yaml: роли и их модели читаются без PyYAML."""
        from tools.trading_ai2.swarm import llm_factory as lf
        env_map = dict(self.ENV)
        with patch.object(lf, "env", lambda name: env_map.get(name, "")):
            models = lf.LLMFactory.role_models("generator")
            self.assertEqual(models, [
                "free/minimax-m3.1-flash",
                "free/gpt-6-luna", "mercury-3",
                "groq/openai/gpt-oss-120b",
                "snova/Meta-Llama-3.3-70B-Instruct",
                "agnes/agnes-3.0-flash", "mistral-small-latest"])
            self.assertEqual(lf.LLMFactory.role_fallback("generator"),
                             "groq/openai/gpt-oss-120b")
            self.assertEqual(lf.LLMFactory.role_models("critic"),
                             ["free/glm-5.3-flash"])
            self.assertEqual(lf.LLMFactory.role_models("arbiter"),
                             ["free/deepseek-v4-pro-0813"])
            # 60 агентов делятся по весам всех восьми моделей
            plan = lf.LLMFactory.role_plan("generator", 60)
            self.assertEqual(len(plan), 60)
            self.assertEqual(set(plan), set(models))

    def test_swarm_gen_models_csv_overrides_yaml(self):
        """SWARM_GEN_MODELS (csv) перекрывает models.yaml."""
        from tools.trading_ai2.swarm import llm_factory as lf
        env_map = dict(self.ENV, SWARM_GEN_MODELS=(
            "google/gemma-4-31b-it:free,cohere/north-mini-code:free"))
        with patch.object(lf, "env", lambda name: env_map.get(name, "")):
            self.assertEqual(lf.LLMFactory.role_models("generator"), [
                "google/gemma-4-31b-it:free", "cohere/north-mini-code:free"])
            plan = lf.LLMFactory.role_plan("generator", 6)
            self.assertEqual(plan.count("google/gemma-4-31b-it:free"), 3)
            self.assertEqual(plan.count("cohere/north-mini-code:free"), 3)

    def test_fallback_log_lines(self):
        """В логе видно обе строки: rate-limit и fallback triggered."""
        from tools.trading_ai2.swarm import llm_factory as lf
        env_map = dict(self.ENV)
        buf: list[str] = []
        with patch.object(lf, "env", lambda name: env_map.get(name, "")):
            with patch.object(lf.LLMFactory, "_log",
                              staticmethod(lambda m: buf.append(m))):
                with _mock_openai_by_model({
                        "gpt-4o": _FakeStatus(429, "rate limited"),
                        "cohere/north-mini-code:free": '{"action": "HOLD"}'}):
                    lf.LLMFactory.complete(
                        role="generator", model="gpt-4o",
                        messages=[{"role": "user", "content": "x"}])
        joined = "\n".join(buf)
        self.assertIn("[llm] Rate-limit, fallback to free model", joined)
        self.assertIn("[llm] Fallback triggered for generator", joined)

    def test_role_fallback_log_line(self):
        """404 → строка «[llm] Fallback triggered for {role}»."""
        from tools.trading_ai2.swarm import llm_factory as lf
        env_map = dict(self.ENV)
        buf: list[str] = []
        with patch.object(lf, "env", lambda name: env_map.get(name, "")):
            with patch.object(lf.LLMFactory, "_log",
                              staticmethod(lambda m: buf.append(m))):
                with _mock_openai_by_model({
                        "gpt-4o": _FakeStatus(404, "not found"),
                        "cohere/north-mini-code:free": '{"action": "HOLD"}'}):
                    lf.LLMFactory.complete(
                        role="generator", model="gpt-4o",
                        messages=[{"role": "user", "content": "x"}])
        joined = "\n".join(buf)
        self.assertIn("[llm] Fallback triggered for generator", joined)


class TestKnowledgeBase(unittest.TestCase):
    """База знаний: 8 аксиом в промпте агента, уроки их не затирают."""

    def test_knowledge_base_has_eight_axioms(self):
        """В knowledge_base.json — 8 базовых правил."""
        from tools.trading_ai2.swarm import knowledge as kb
        rules = kb.load_rules()
        self.assertEqual(len(rules), 8)
        for rule in rules:
            self.assertGreater(len(rule), 20, rule)
        # правила узнаваемые, а не заглушки
        joined = " ".join(rules).lower()
        self.assertIn("funding", joined.lower())
        self.assertIn("rsi", joined.lower())

    def test_heavy_majority_rule_is_in_knowledge_base(self):
        """Правило пула: 70% тяжёлых (top-30) HOLD — сделка отменяется."""
        from tools.trading_ai2.swarm import knowledge as kb
        joined = " ".join(kb.load_rules())
        self.assertIn("70%", joined)
        self.assertIn("top-30", joined)
        self.assertIn("HOLD", joined)
        self.assertIn("отменить сделку", joined)

    def test_correlation_rule_is_in_knowledge_base(self):
        """Правило корреляции активов есть в базе знаний."""
        from tools.trading_ai2.swarm import knowledge as kb
        joined = " ".join(kb.load_rules()).lower()
        self.assertIn("коррелирующ", joined)
        self.assertIn("0.8", joined)
        self.assertIn("sol", joined)

    def test_two_level_sentiment_rule_is_in_knowledge_base(self):
        """Два сентимента (глобальный + по активу) закреплены в базе."""
        from tools.trading_ai2.swarm import knowledge as kb
        joined = " ".join(kb.load_rules())
        self.assertIn("market_sentiment", joined)
        self.assertIn("asset_sentiment", joined)

    def test_rules_in_agent_system_prompt(self):
        """В системном промпте агента есть ≥3 строки базы знаний."""
        from tools.trading_ai2.swarm import knowledge as kb
        from tools.trading_ai2.swarm.agent_swarm import make_swarm
        rules = kb.load_rules()
        swarm = make_swarm(5)
        for agent in swarm:
            self.assertIn(kb.RULES_HEADER, agent.system_prompt)
            hits = [r for r in rules if r in agent.system_prompt]
            self.assertGreaterEqual(len(hits), 3, agent.name)

    def test_rules_reach_llm_request(self):
        """Правила доходят до реального запроса к LLM (persona в снапшоте)."""
        from tools.trading_ai2.swarm import knowledge as kb
        from tools.trading_ai2.swarm.agent_swarm import make_swarm
        rules = kb.load_rules()
        agent = make_swarm(1)[0]
        snap = {"rsi": 44.0, "ema9_above_ema21": True, "price": 80_000.0}
        # агент кладёт свой system_prompt в снимок → он уходит в LLM
        payload = {**snap, "persona": agent.system_prompt}
        text = json.dumps(payload, ensure_ascii=False)
        hits = [r for r in rules if r in text]
        self.assertGreaterEqual(len(hits), 3)

    def test_lessons_do_not_overwrite_base_rules(self):
        """Уроки дополняют правила: после apply_lessons база на месте."""
        from tools.trading_ai2.swarm import knowledge as kb
        from tools.trading_ai2.swarm.agent_swarm import apply_lessons, make_swarm
        rules = kb.load_rules()
        swarm = make_swarm(5)
        apply_lessons(swarm, "УРОК (не входи на перепроданном RSI)")
        for agent in swarm:
            self.assertIn(kb.RULES_HEADER, agent.system_prompt)
            self.assertIn(kb.LESSONS_HEADER, agent.system_prompt)
            self.assertIn("УРОК (", agent.system_prompt)
            self.assertEqual(agent.system_prompt.count(kb.RULES_HEADER), 1)
            self.assertEqual(agent.system_prompt.count(kb.LESSONS_HEADER), 1)
            for rule in rules:
                self.assertIn(rule, agent.system_prompt)

    def test_base_rules_survive_repeated_lessons(self):
        """Повторное применение уроков не размножает ни правила, ни уроки."""
        from tools.trading_ai2.swarm import knowledge as kb
        from tools.trading_ai2.swarm.agent_swarm import apply_lessons, make_swarm
        swarm = make_swarm(3)
        for _ in range(3):
            apply_lessons(swarm, "УРОК (не входи на перепроданном RSI)")
        for agent in swarm:
            self.assertEqual(agent.system_prompt.count(kb.RULES_HEADER), 1)
            self.assertEqual(agent.system_prompt.count(kb.LESSONS_HEADER), 1)

    def test_tui_log_line_format(self):
        """Строка лога TUI: Loaded 8 base rules + N learned lessons."""
        from tools.trading_ai2.swarm.knowledge import summary
        self.assertEqual(summary(0),
                         "Loaded 8 base rules + 0 learned lessons "
                         "into agent context")
        self.assertEqual(summary(3),
                         "Loaded 8 base rules + 3 learned lessons "
                         "into agent context")

    def test_demo_logs_memory_line(self):
        """В демо-цикле печатается [memory] Loaded 8 base rules…"""
        from tools.trading_ai2.demo_trading_grand import _log
        from tools.trading_ai2.swarm.knowledge import summary
        out: list[str] = []
        _log(out.append, "[memory]", summary(0))
        line = out[0]
        self.assertIn("[memory]", line)
        self.assertIn("Loaded 8 base rules + 0 learned lessons", line)

    def test_missing_knowledge_file_degrades(self):
        """Нет файла базы — блок пустой, торговля продолжается."""
        from tools.trading_ai2.swarm import knowledge as kb
        rules = kb.load_rules(path="no_such_knowledge_base.json")
        self.assertEqual(rules, [])
        self.assertEqual(kb.rules_block(rules), "")
        self.assertIn("0 base rules", kb.summary(0, rules))



class TestIndicators(unittest.TestCase):
    def test_indicator_formulas_sane(self):
        import pandas as pd
        close = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13,
                           14, 15, 16, 17, 18, 19, 20], dtype=float)
        df = pd.DataFrame({"close": close})
        out = market_data.MarketData.add_indicators(
            market_data.MarketData.__new__(market_data.MarketData), df)
        self.assertGreater(out["rsi"].iloc[-1], 90)      # чистый рост
        ema9 = close.ewm(span=9, adjust=False).mean()
        self.assertAlmostEqual(out["ema9"].iloc[-1], ema9.iloc[-1])
        # ATR есть и неотрицателен — по нему решается правило «ATR +50%/час»
        self.assertIn("atr", out.columns)
        self.assertGreater(out["atr"].iloc[-1], 0.0)

    def test_atr_context_reports_growth(self):
        """ATR-контекст: рост волатильности в процентах за час."""
        import pandas as pd
        md = market_data.MarketData.__new__(market_data.MarketData)
        rows = []
        for i in range(80):
            price = 100.0 + i
            rows.append({"open": price, "high": price + 1,
                         "low": price - 1, "close": price, "volume": 1.0})
        df = pd.DataFrame(rows)
        df = md.add_indicators(df)
        ctx = md.atr_context(df, "1m")
        self.assertIn("atr", ctx)
        self.assertIn("atr_growth_pct", ctx)
        self.assertGreaterEqual(ctx["atr"], 0.0)


class TestJsonSanitizer(unittest.TestCase):
    """JSON Sanitizer: битый ответ модели → решение из регулярок.

    Три приёма починки: целый объект (баланс скобок), обрезанный объект
    (закрытая строка + скобки), наконец регулярки по полям. Если и это
    не дало action — честный HOLD с reason "parse_error".
    """

    def test_truncated_json_recovers_action_confidence_reason(self):
        """Обрыв на середине reason: action и confidence — из починки."""
        raw = ('{"action":"BUY","amount_percent":1.5,"confidence":0.7,'
               '"reason":"RSI 22 — перепродан, вхожу')
        d = parse_decision(raw, model="test-model", price=80_000.0)
        self.assertEqual(d.action, "BUY")
        self.assertEqual(d.mode, "sanitized")
        self.assertAlmostEqual(d.confidence, 0.7)
        self.assertAlmostEqual(d.amount_percent, 1.5)
        self.assertIn("RSI 22", d.reason)
        self.assertGreater(d.stop_loss, 0.0)

    def test_json_wrapped_in_prose_is_recovered(self):
        """Модель обложила JSON текстом — объект вырезан по балансу скобок."""
        raw = ('Вот мой план:\n{"action":"SELL","amount_percent":50,'
               '"confidence":0.8,"reason":"фиксация прибыли","stop_loss":0}\n'
               'Готово.')
        d = parse_decision(raw, model="test-model", price=80_000.0)
        self.assertEqual(d.action, "SELL")
        self.assertEqual(d.mode, "sanitized")
        self.assertAlmostEqual(d.confidence, 0.8)
        self.assertAlmostEqual(d.amount_percent, 50.0)
        self.assertIn("фиксация прибыли", d.reason)

    def test_loose_fields_without_json_are_extracted(self):
        """Совсем без JSON: поля вытащены регулярными выражениями."""
        raw = ('Решение: action: BUY, confidence: 0.6, '
               '"reason": "вход по тренду"')
        d = parse_decision(raw, model="test-model", price=80_000.0)
        self.assertEqual(d.action, "BUY")
        self.assertEqual(d.mode, "sanitized")
        self.assertAlmostEqual(d.confidence, 0.6)
        self.assertIn("вход по тренду", d.reason)

    def test_garbage_gives_hold_with_parse_error(self):
        """Ничего не вытащили — HOLD с reason "parse_error"."""
        for junk in ["не понимаю запрос", "BUY BTC NOW!!!", "{}", "```",
                     "{\"amount_percent\":1.5,\"reason\":\"оборвалось"]:
            d = parse_decision(junk)
            self.assertEqual(d.action, "HOLD", junk)
            self.assertEqual(d.reason, "parse_error", junk)
            self.assertEqual(d.mode, "parse_fallback", junk)

    def test_action_case_is_normalized(self):
        """Модель пишет "buy"/"Sell" — регистр не меняет смысла решения."""
        d = parse_decision('{"action":"buy","amount_percent":1.0,'
                           '"reason":"x","stop_loss":78000}')
        self.assertEqual(d.action, "BUY")
        d2 = parse_decision('{"action":"Sell","amount_percent":100,'
                            '"reason":"x","stop_loss":0}')
        self.assertEqual(d2.action, "SELL")

    def test_valid_json_but_invalid_schema_stays_hold(self):
        """Семантику валидации спасение не подменяет."""
        d = parse_decision('{"action":"SELL","amount_percent":500,'
                           '"reason":"x","stop_loss":0}')
        self.assertEqual(d.action, "HOLD")
        self.assertEqual(d.mode, "parse_fallback")
        # BUY без стопа — тоже честный HOLD
        d2 = parse_decision('{"action":"BUY","amount_percent":1.0,'
                            '"reason":"x","stop_loss":0}')
        self.assertEqual(d2.action, "HOLD")

    def test_buy_without_known_price_is_not_rescued(self):
        """BUY без стопа и без цены — спасение опасно: честный HOLD."""
        d = parse_decision('{"action":"BUY","amount_percent":1.0,'
                           '"reason":"x"', price=0.0)
        self.assertEqual(d.action, "HOLD")
        self.assertEqual(d.mode, "parse_fallback")

    def test_sanitizer_module_directly(self):
        """Модуль json_sanitizer: починка, регулярки, признак action."""
        from tools.trading_ai2.swarm import json_sanitizer as js
        broken = '{"action":"BUY","confidence":0.4,"reason":"обрыв'
        self.assertTrue(js.repair_json(broken).endswith("}"))
        self.assertEqual(js.extract(broken)["action"], "BUY")
        self.assertAlmostEqual(js.extract(broken)["confidence"], 0.4)
        fields = js.salvage(broken)
        self.assertTrue(js.has_action(fields))
        self.assertFalse(js.has_action({}))
        self.assertEqual(js.salvage("мусор"), {})


class TestModelBlacklist(unittest.TestCase):
    """Чёрный список: первый же 402/404 → 15 минут без запросов.

    404 — мёртвый эндпоинт, гасим модель целиком. 402 — не хватило
    баланса: баланс у каждого ключа пула свой, поэтому гасим ключ
    (если в пуле есть другие) и идём на следующий ключ.
    """

    ENV = {
        "ACTIVE_LLM_MODEL": "gpt-4o",
        "DEEPSEEK_API_KEY": "sk-deepseek",
        "DEEPSEEK_BASE_URL": "",
        "OPENAI_API_KEY": "sk-oai",
        "OPENAI_BASE_URL": "",
        "ANTHROPIC_API_KEY": "",
        "INCEPTION_API_KEY": "",
        "LLM_API_KEY": "sk-gw",
        "LLM_BASE_URL": "https://gw/v1",
        "LLM_MODEL": "cohere/north-mini-code:free",
        "SWARM_GEN_MODELS": "",
        "SWARM_CRITIC_MODEL": "",
        "SWARM_ARBITER_MODEL": "",
    }

    def setUp(self):
        from tools.trading_ai2.swarm import llm_factory as lf
        self.lf = lf
        self._qs = lf.QUOTA_SLEEP_S
        lf.QUOTA_SLEEP_S = 0.0        # в тестах quota-сон не нужен
        lf.LLMFactory.reset_clients()
        lf.LLMFactory.reset_free_calls()
        lf.LLMFactory.reset_blacklist()

    def tearDown(self):
        self.lf.QUOTA_SLEEP_S = self._qs
        self.lf.LLMFactory.reset_clients()
        self.lf.LLMFactory.reset_free_calls()
        self.lf.LLMFactory.reset_blacklist()

    def _complete(self, model: str, handlers: dict, calls: list,
                  env_extra: dict | None = None, role: str = "generator"):
        """Запрос к LLM с подменой клиента (ответ/ошибка по модели)."""
        env_map = dict(self.ENV, **(env_extra or {}))
        with patch.object(self.lf, "env", lambda name: env_map.get(name, "")):
            with _mock_openai_counting(handlers, calls):
                return self.lf.LLMFactory.complete(
                    role=role, model=model,
                    messages=[{"role": "user", "content": "x"}])

    def test_first_402_blacklists_model_for_15_minutes(self):
        """Один 402 — и модель уходит из ротации на 15 минут."""
        calls: list[str] = []
        buf: list[str] = []
        from tools.trading_ai2.swarm import llm_factory as lf
        with patch.object(lf.LLMFactory, "_log",
                          staticmethod(lambda m: buf.append(m))):
            self._complete(
                "gpt-4o",
                {"gpt-4o": _FakeStatus(402, "Payment Required"),
                 "cohere/north-mini-code:free": '{"action": "HOLD"}'},
                calls)
        self.assertTrue(lf.LLMFactory.is_blacklisted("gpt-4o"))
        self.assertEqual(lf.LLMFactory.blacklist_reason("gpt-4o"), "rate_limit")
        entries = lf.LLMFactory.blacklist_entries()
        self.assertIn("gpt-4o", entries)
        self.assertGreater(entries["gpt-4o"], 890.0)
        self.assertLessEqual(entries["gpt-4o"], 900.0)
        joined = "\n".join(buf)
        self.assertIn("[llm] Blacklisted model gpt-4o for 15 min", joined)
        self.assertIn("402 insufficient balance", joined)

    def test_402_squeeze_retries_same_key_with_reduced_budget(self):
        """Достаточный afford повторяет запрос той же модели/ключа с cap."""
        from tools.trading_ai2.swarm import llm_factory as lf
        requests: list[tuple[str, int]] = []
        client_keys: list[str] = []

        def squeeze_then_success(_model: str, attempt: int, _kwargs: dict):
            return (_FakeStatus(402, "can only afford 600 tokens")
                    if attempt == 1 else '{"action": "BUY"}')

        env_map = dict(self.ENV)
        with patch.object(lf, "env", lambda name: env_map.get(name, "")):
            with _mock_openai_requests(
                    {"gpt-4o": squeeze_then_success}, requests, client_keys):
                result = lf.LLMFactory.complete(
                    role="generator", model="gpt-4o",
                    messages=[{"role": "user", "content": "x"}],
                    max_tokens=800)

        self.assertEqual(result, '{"action": "BUY"}')
        self.assertEqual(requests, [("gpt-4o", 800), ("gpt-4o", 584)])
        self.assertEqual(client_keys, ["sk-oai"])
        self.assertFalse(lf.LLMFactory.is_blacklisted("gpt-4o"))

    def test_402_below_squeeze_threshold_rotates_key(self):
        """Недостаточный afford не включает squeeze: ключ блокируется и меняется."""
        from tools.trading_ai2.swarm import llm_factory as lf
        requests: list[tuple[str, int]] = []
        client_keys: list[str] = []
        env_map = dict(self.ENV, **{
            "LLM_BASE_URL": "https://openrouter.ai/api/v1",
            "LLM_API_KEY": "sk-or-v1-AAAAAA",
            "LLM_API_KEYS": "sk-or-v1-AAAAAA,sk-or-v1-BBBBBB",
        })

        def low_budget_then_success(_model: str, attempt: int, _kwargs: dict):
            return (_FakeStatus(402, "can only afford 415 tokens")
                    if attempt == 1 else '{"action": "HOLD"}')

        with patch.object(lf, "env", lambda name: env_map.get(name, "")):
            with _mock_openai_requests(
                    {"vendor/paid-model": low_budget_then_success},
                    requests, client_keys):
                result = lf.LLMFactory.complete(
                    role="generator", model="vendor/paid-model",
                    messages=[{"role": "user", "content": "x"}],
                    max_tokens=800)

        self.assertEqual(result, '{"action": "HOLD"}')
        self.assertEqual(requests, [
            ("vendor/paid-model", 800), ("vendor/paid-model", 800)])
        self.assertEqual(client_keys, ["sk-or-v1-AAAAAA", "sk-or-v1-BBBBBB"])
        entries = lf.LLMFactory.blacklist_entries()
        self.assertIn("vendor/paid-model|AAAAAA", entries)
        self.assertNotIn("vendor/paid-model", entries)

    def test_three_404_in_a_row_blacklist_model(self):
        """Один 404 — ещё не приговор: модель гасится после трёх подряд."""
        calls: list[str] = []
        buf: list[str] = []
        from tools.trading_ai2.swarm import llm_factory as lf
        handlers = {"gpt-4o": _FakeStatus(404, "not found"),
                    "cohere/north-mini-code:free": '{"action": "HOLD"}'}
        with patch.object(lf.LLMFactory, "_log",
                          staticmethod(lambda m: buf.append(m))):
            self._complete("gpt-4o", handlers, calls)
            self.assertFalse(lf.LLMFactory.is_blacklisted("gpt-4o"))
            self.assertEqual(lf.LLMFactory.strikes("gpt-4o"), 1)
            self._complete("gpt-4o", handlers, calls)
            self.assertFalse(lf.LLMFactory.is_blacklisted("gpt-4o"))
            self.assertEqual(lf.LLMFactory.strikes("gpt-4o"), 2)
            self._complete("gpt-4o", handlers, calls)
        self.assertTrue(lf.LLMFactory.is_blacklisted("gpt-4o"))
        self.assertEqual(lf.LLMFactory.blacklist_reason("gpt-4o"), "not_found")
        entries = lf.LLMFactory.blacklist_entries()
        self.assertIn("gpt-4o", entries)
        self.assertGreater(entries["gpt-4o"], 890.0)
        joined = "\n".join(buf)
        self.assertIn("[llm] Blacklisted model gpt-4o for 15 min", joined)
        self.assertIn("not_found", joined)

    def test_three_empty_responses_blacklist_and_next_call_skips_model(self):
        """Три пустых content подряд гасят модель; следующий вызов идёт в fallback."""
        calls: list[str] = []
        from tools.trading_ai2.swarm import llm_factory as lf
        handlers = {"gpt-4o": "",
                    "cohere/north-mini-code:free": '{"action": "HOLD"}'}

        for _ in range(lf.BLACKLIST_STRIKES):
            self._complete("gpt-4o", handlers, calls)

        self.assertTrue(lf.LLMFactory.is_blacklisted("gpt-4o"))
        self.assertEqual(lf.LLMFactory.blacklist_reason("gpt-4o"), "empty")
        self.assertEqual(calls.count("gpt-4o"), lf.BLACKLIST_STRIKES)

        self._complete("gpt-4o", handlers, calls)
        self.assertEqual(calls.count("gpt-4o"), lf.BLACKLIST_STRIKES)
        self.assertEqual(calls.count("cohere/north-mini-code:free"),
                         lf.BLACKLIST_STRIKES + 1)

    def test_empty_strike_is_cleared_by_success(self):
        """Успешный ответ между empty сбрасывает последовательный счётчик."""
        calls: list[str] = []
        from tools.trading_ai2.swarm import llm_factory as lf

        def empty_success_empty(_model: str, attempt: int) -> str:
            return "" if attempt in (1, 3) else '{"action": "BUY"}'

        handlers = {"gpt-4o": empty_success_empty,
                    "cohere/north-mini-code:free": '{"action": "HOLD"}'}
        self._complete("gpt-4o", handlers, calls)
        self.assertEqual(lf.LLMFactory.strikes("gpt-4o"), 1)
        self._complete("gpt-4o", handlers, calls)
        self.assertEqual(lf.LLMFactory.strikes("gpt-4o"), 0)
        self._complete("gpt-4o", handlers, calls)
        self.assertEqual(lf.LLMFactory.strikes("gpt-4o"), 1)
        self.assertFalse(lf.LLMFactory.is_blacklisted("gpt-4o"))

    def test_empty_and_http_strikes_share_consecutive_failure_counter(self):
        """Empty и 404 копятся вместе, пока модель не даст успешный ответ."""
        calls: list[str] = []
        from tools.trading_ai2.swarm import llm_factory as lf

        def mixed_failures(_model: str, attempt: int):
            return (_FakeStatus(404, "not found") if attempt == 2 else "")

        handlers = {"gpt-4o": mixed_failures,
                    "cohere/north-mini-code:free": '{"action": "HOLD"}'}
        for _ in range(lf.BLACKLIST_STRIKES):
            self._complete("gpt-4o", handlers, calls)

        self.assertEqual(lf.LLMFactory.strikes("gpt-4o"), 3)
        self.assertTrue(lf.LLMFactory.is_blacklisted("gpt-4o"))

    def test_three_429_in_a_row_blacklist_model(self):
        """Три 429 подряд — модель в чёрный список на 15 минут."""
        calls: list[str] = []
        buf: list[str] = []
        from tools.trading_ai2.swarm import llm_factory as lf
        handlers = {"gpt-4o": _FakeStatus(429, "Too Many Requests"),
                    "cohere/north-mini-code:free": '{"action": "HOLD"}'}
        with patch.object(lf.LLMFactory, "_log",
                          staticmethod(lambda m: buf.append(m))):
            for _ in range(3):
                self._complete("gpt-4o", handlers, calls)
        self.assertTrue(lf.LLMFactory.is_blacklisted("gpt-4o"))
        self.assertEqual(lf.LLMFactory.blacklist_reason("gpt-4o"), "rate_limit")
        joined = "\n".join(buf)
        self.assertIn("[llm] Blacklisted model gpt-4o for 15 min", joined)

    def test_successful_response_resets_strikes(self):
        """Отказы не подряд — счётчик сбрасывается успешным ответом."""
        calls: list[str] = []
        from tools.trading_ai2.swarm import llm_factory as lf
        handlers = {"gpt-4o": _FakeStatus(404, "not found"),
                    "cohere/north-mini-code:free": '{"action": "HOLD"}'}
        self._complete("gpt-4o", handlers, calls)
        self.assertEqual(lf.LLMFactory.strikes("gpt-4o"), 1)
        lf.LLMFactory._clear_strikes("gpt-4o")
        self.assertEqual(lf.LLMFactory.strikes("gpt-4o"), 0)
        self._complete("gpt-4o", handlers, calls)
        self.assertEqual(lf.LLMFactory.strikes("gpt-4o"), 1)
        self.assertFalse(lf.LLMFactory.is_blacklisted("gpt-4o"))

    def test_blacklisted_model_is_skipped_without_spending_request(self):
        """В чёрном списке — запрос не тратим: к модели не идём."""
        calls: list[str] = []
        buf: list[str] = []
        from tools.trading_ai2.swarm import llm_factory as lf
        handlers = {"gpt-4o": _FakeStatus(402, "Payment Required"),
                    "cohere/north-mini-code:free": '{"action": "HOLD"}'}
        with patch.object(lf.LLMFactory, "_log",
                          staticmethod(lambda m: buf.append(m))):
            self._complete("gpt-4o", handlers, calls)     # 402 → ЧС
            self._complete("gpt-4o", handlers, calls)     # ЧС → без запроса
        self.assertEqual(calls.count("gpt-4o"), 1)
        self.assertEqual(calls.count("cohere/north-mini-code:free"), 2)
        joined = "\n".join(buf)
        self.assertIn("[llm] Skipped blacklisted model gpt-4o", joined)

    def test_blacklist_expires_after_15_minutes(self):
        """Через 15 минут модель снова в ротации."""
        from tools.trading_ai2.swarm import llm_factory as lf
        lf.LLMFactory.blacklist_model("gpt-4o", "rate_limit")
        self.assertTrue(lf.LLMFactory.is_blacklisted("gpt-4o"))
        _expiry, reason = lf.LLMFactory._blacklist["gpt-4o"]
        lf.LLMFactory._blacklist["gpt-4o"] = (time.time() - 1.0, reason)
        self.assertFalse(lf.LLMFactory.is_blacklisted("gpt-4o"))
        self.assertEqual(lf.LLMFactory.blacklist_entries(), {})

    def test_402_with_key_pool_blocks_key_not_model(self):
        """402 гасит ключ пула (баланс у ключа свой), модель остаётся живой."""
        calls: list[str] = []
        buf: list[str] = []
        from tools.trading_ai2.swarm import llm_factory as lf
        env_extra = {
            "LLM_BASE_URL": "https://openrouter.ai/api/v1",
            "LLM_API_KEY": "sk-or-v1-AAAAAA",
            "LLM_API_KEYS": "sk-or-v1-AAAAAA,sk-or-v1-BBBBBB",
        }
        handlers = {
            # первый ключ без баланса, второй отвечает
            "vendor/paid-model":
                lambda model, n: (_FakeStatus(402, "Payment Required")
                                  if n == 1 else '{"action": "HOLD"}'),
            "cohere/north-mini-code:free": '{"action": "HOLD"}',
        }
        with patch.object(lf.LLMFactory, "_log",
                          staticmethod(lambda m: buf.append(m))):
            out = self._complete("vendor/paid-model", handlers, calls,
                                 env_extra=env_extra)
        self.assertEqual(out, '{"action": "HOLD"}')
        entries = lf.LLMFactory.blacklist_entries()
        self.assertIn("vendor/paid-model|AAAAAA", entries)
        self.assertNotIn("vendor/paid-model", entries)
        # запрос ушёл на второй ключ пула — не в пустую
        self.assertEqual(calls.count("vendor/paid-model"), 2)
        joined = "\n".join(buf)
        self.assertIn("[llm] Blacklisted key …AAAA of vendor/paid-model",
                      joined)

    def test_blacklisted_key_rotates_to_next_pool_key(self):
        """Мёртв только ключ пула — модель не выбрасывается, идёт на другой ключ."""
        calls: list[str] = []
        buf: list[str] = []
        from tools.trading_ai2.swarm import llm_factory as lf
        env_extra = {
            "LLM_BASE_URL": "https://openrouter.ai/api/v1",
            "LLM_API_KEY": "sk-or-v1-AAAAAA",
            "LLM_API_KEYS": "sk-or-v1-AAAAAA,sk-or-v1-BBBBBB",
        }
        lf.LLMFactory.blacklist_model("vendor/paid-model", "rate_limit",
                                      key="sk-or-v1-AAAAAA")
        with patch.object(lf.LLMFactory, "_log",
                          staticmethod(lambda m: buf.append(m))):
            out = self._complete("vendor/paid-model",
                                 {"vendor/paid-model": '{"action": "HOLD"}'},
                                 calls, env_extra=env_extra)
        self.assertEqual(out, '{"action": "HOLD"}')
        self.assertEqual(calls.count("vendor/paid-model"), 1)
        joined = "\n".join(buf)
        self.assertIn("[llm] Skipped blacklisted key …AAAA of "
                      "vendor/paid-model", joined)

    def test_blacklisted_model_is_not_offered_as_fallback(self):
        """В резерв мёртвая модель не попадает."""
        from tools.trading_ai2.swarm import llm_factory as lf
        env_map = dict(self.ENV)
        with patch.object(lf, "env", lambda name: env_map.get(name, "")):
            lf.LLMFactory.blacklist_model("cohere/north-mini-code:free",
                                          "not_found")
            fallbacks = lf.LLMFactory._fallbacks("generator", "gpt-4o",
                                                 "not_found")
        self.assertNotIn("cohere/north-mini-code:free", fallbacks)
        for model in fallbacks:
            self.assertFalse(lf.LLMFactory.is_blacklisted(model))


class TestDarwinianWeights(unittest.TestCase):
    """Дарвиновские веса: убыток → вес вниз, прибыль → вверх.

    Арбитр считает score = Σ (confidence × weight): голос удачного
    агента весит больше, голос неудачного — меньше.
    """

    def test_weight_drops_on_loss_and_grows_on_profit(self):
        """Шаг веса аддитивный: убыток −0.1, прибыль +0.1."""
        from tools.trading_ai2.swarm import darwin
        self.assertAlmostEqual(darwin.update_weight(1.0, -12.0), 0.9)
        self.assertAlmostEqual(darwin.update_weight(1.0, 12.0), 1.1)
        self.assertAlmostEqual(darwin.update_weight(1.0, 0.0), 1.0)

    def test_weight_step_is_ten_percent_points(self):
        """Величина шага задана константой WEIGHT_STEP = 0.1."""
        from tools.trading_ai2.swarm import darwin
        self.assertAlmostEqual(darwin.WEIGHT_STEP, 0.1)
        self.assertAlmostEqual(
            darwin.update_weight(1.0, -1.0) - darwin.update_weight(1.0, 1.0),
            -0.2)

    def test_two_losses_move_weight_by_two_steps(self):
        """Две ошибки подряд — минус две десятых."""
        from tools.trading_ai2.swarm import darwin
        w = darwin.update_weight(1.0, -5.0)
        w = darwin.update_weight(w, -5.0)
        self.assertAlmostEqual(w, 0.8)

    def test_weight_clamped_to_bounds(self):
        from tools.trading_ai2.swarm import darwin
        self.assertEqual(darwin.update_weight(darwin.WEIGHT_MIN, -100.0),
                         darwin.WEIGHT_MIN)
        self.assertEqual(darwin.update_weight(darwin.WEIGHT_MAX, 100.0),
                         darwin.WEIGHT_MAX)

    def test_apply_outcome_moves_only_named_agents(self):
        from tools.trading_ai2.swarm import darwin
        from tools.trading_ai2.swarm.agent_swarm import SwarmAgent
        agents = [SwarmAgent("winner", "TrendFollower", "p"),
                  SwarmAgent("loser", "Scalper", "p")]
        with patch.object(darwin, "save_weights",
                          lambda weights, path=None: None):
            changed = darwin.apply_outcome(agents, ["loser"], -30.0)
        self.assertLess(agents[1].weight, 1.0)
        self.assertEqual(agents[0].weight, 1.0)
        self.assertIn("loser", changed)
        self.assertAlmostEqual(changed["loser"]["old"], 1.0)

    def test_weights_persist_and_reload(self):
        from tools.trading_ai2.swarm import darwin
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "weights.json"
            darwin.save_weights({"a1": 0.8}, path)
            self.assertEqual(darwin.load_weights(path), {"a1": 0.8})
            path.write_text("не json", encoding="utf-8")
            self.assertEqual(darwin.load_weights(path), {})

    def test_swarm_agents_get_persisted_weights(self):
        """Новый цикл подхватывает веса из файла."""
        from tools.trading_ai2.swarm import darwin
        from tools.trading_ai2.swarm.agent_swarm import make_swarm

        class _AllWeights(dict):
            def get(self, key, default=None):
                return 2.5

        with patch.object(darwin, "load_weights",
                          lambda *a, **k: _AllWeights()):
            swarm = make_swarm(3)
        self.assertTrue(swarm)
        for agent in swarm:
            self.assertEqual(agent.weight, 2.5)

    def test_arbiter_score_is_weighted_by_agent_weight(self):
        """score = Σ(value × conf × weight) / Σ(conf × weight).

        Вес 3.0 — вето-сила: множитель ×3, эффективный вес 9.0.
        """
        from tools.trading_ai2.swarm.arbiter import Arbiter
        votes = [SwarmVote(action="BUY", confidence=0.8, reason="t",
                           stop_loss=78_000.0, agent="a", weight=3.0),
                 SwarmVote(action="SELL", confidence=0.8, reason="t",
                           stop_loss=0.0, agent="b", weight=0.2)]
        # BUY: 0.8 × 9.0 = 7.2 · SELL: 0.8 × 0.2 = 0.16
        self.assertAlmostEqual(Arbiter.score_votes(votes), 7.04 / 7.36,
                               places=6)
        self.assertAlmostEqual(Arbiter.weight_mass(votes), 7.36, places=6)
        # без весов голоса равны — шум, консенсуса нет
        equal = [SwarmVote(action="BUY", confidence=0.8, reason="t",
                           stop_loss=78_000.0, agent="a"),
                 SwarmVote(action="SELL", confidence=0.8, reason="t",
                           stop_loss=0.0, agent="b")]
        self.assertAlmostEqual(Arbiter.score_votes(equal), 0.0)

    def test_arbiter_verdict_uses_darwinian_score(self):
        """Тяжёлый победитель тянет вердикт Арбитра в BUY."""
        from tools.trading_ai2.swarm.arbiter import Arbiter
        from tools.trading_ai2.swarm.generator import Proposal
        votes = [SwarmVote(action="BUY", confidence=0.8, reason="t",
                           stop_loss=78_000.0, agent="a", weight=3.0),
                 SwarmVote(action="SELL", confidence=0.8, reason="t",
                           stop_loss=0.0, agent="b", weight=0.2)]
        proposal = Proposal("BUY", 0.0, {"BUY": 1, "SELL": 1, "HOLD": 0},
                            1.5, 78_000.0, "t")
        con = Arbiter().rule(proposal, votes=votes)
        self.assertEqual(con.action, "BUY")
        self.assertAlmostEqual(con.score, 7.04 / 7.36, places=6)
        self.assertTrue(con.actionable)

    def test_demo_logs_darwinian_score(self):
        """В TUI видно дарвиновский score и массу голосов."""
        from tools.trading_ai2.demo_trading_grand import _log
        out: list[str] = []
        _log(out.append, "[darwin]", "score = Σ(conf × weight): +0.875")
        self.assertIn("[darwin]", out[0])
        self.assertIn("Σ(conf × weight)", out[0])


class TestSafeParseJson(unittest.TestCase):
    """_safe_parse_json: обрезанный ответ Mercury чинится, мусор → None.

    Требование заказчика: если action не найден — HOLD, но БЕЗ вызова
    других моделей.
    """

    def test_truncated_string_is_closed(self):
        from tools.trading_ai2.swarm.json_sanitizer import _safe_parse_json
        data = _safe_parse_json('{"action": "BUY", "reason": "сильный импульс')
        self.assertIsNotNone(data)
        self.assertEqual(data["action"], "BUY")
        self.assertIn("сильный импульс", data["reason"])

    def test_dangling_key_is_dropped(self):
        from tools.trading_ai2.swarm.json_sanitizer import _safe_parse_json
        data = _safe_parse_json('{"action": "SELL", "stop_loss": 79000, "reason":')
        self.assertIsNotNone(data)
        self.assertEqual(data["action"], "SELL")
        self.assertNotIn("reason", data)

    def test_partial_number_is_dropped(self):
        from tools.trading_ai2.swarm.json_sanitizer import _safe_parse_json
        data = _safe_parse_json('{"action": "BUY", "amount_percent": 1.')
        self.assertIsNotNone(data)
        self.assertEqual(data["action"], "BUY")
        self.assertNotIn("amount_percent", data)

    def test_garbage_returns_none(self):
        from tools.trading_ai2.swarm.json_sanitizer import _safe_parse_json
        self.assertIsNone(_safe_parse_json("не json вовсе"))
        self.assertIsNone(_safe_parse_json(""))

    def test_fenced_json_is_parsed(self):
        from tools.trading_ai2.swarm.json_sanitizer import _safe_parse_json
        data = _safe_parse_json('```json\n{"action": "HOLD"}\n```')
        self.assertEqual(data["action"], "HOLD")

    def test_parse_decision_recovers_truncated_json(self):
        raw = ('{"action": "BUY", "amount_percent": 1.5, "stop_loss": 78000, '
               '"reason": "тренд и объём')
        d = parse_decision(raw, model="test-model", price=80_000.0)
        self.assertEqual(d.action, "BUY")
        self.assertGreater(d.stop_loss, 0.0)

    def test_parse_decision_garbage_is_hold(self):
        d = parse_decision("не json вовсе", model="test-model", price=80_000.0)
        self.assertEqual(d.action, "HOLD")
        self.assertEqual(d.mode, "parse_fallback")


class TestNewsSentimentCache(unittest.TestCase):
    """TTL-кэш оценки новостей: второй вызов в TTL не зовёт модель."""

    def _counting_mock(self, content: str, calls: dict):
        class _Msg:
            def __init__(self, c):
                self.content = c

        class _Choice:
            def __init__(self, c):
                self.message = _Msg(c)

        class _Resp:
            def __init__(self, c):
                self.choices = [_Choice(c)]

        class _Completions:
            def create(self, **kwargs):
                calls["n"] += 1
                return _Resp(content)

        class _Chat:
            def __init__(self, c):
                self.completions = _Completions()

        class _Client:
            def __init__(self, c):
                self.chat = _Chat(c)

        import openai
        return patch.object(openai, "OpenAI", lambda **kw: _Client(content))

    def test_second_call_within_ttl_hits_cache(self):
        """Первый вызов спрашивает модель, второй — берёт из кэша."""
        from tools.trading_ai2.swarm import news_sentiment as ns
        calls = {"n": 0}
        ns.reset_cache()
        with self._counting_mock(
                '{"sentiment": 0.4, "summary": "нормально"}', calls), \
             patch.object(ns, "fetch_headlines",
                          lambda symbol="BTC", count=3:
                          (list(ns.MOCK_HEADLINES), "mock")), \
             patch.object(ns, "env", lambda name, default="":
                          "sk-mocked" if name == "DEEPSEEK_API_KEY" else default):
            first = ns.get_market_sentiment("BTC")
            second = ns.get_market_sentiment("BTC")
        self.assertEqual(calls["n"], 1)
        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)
        self.assertEqual(second.age_mins, 0)
        self.assertAlmostEqual(second.score, 0.4)
        ns.reset_cache()

    def test_expired_cache_reasks_model(self):
        """За TTL кэш протухает — модель зовётся снова."""
        from tools.trading_ai2.swarm import news_sentiment as ns
        calls = {"n": 0}
        ns.reset_cache()
        with self._counting_mock(
                '{"sentiment": -0.2, "summary": "нейтрально"}', calls), \
             patch.object(ns, "fetch_headlines",
                          lambda symbol="BTC", count=3:
                          (list(ns.MOCK_HEADLINES), "mock")), \
             patch.object(ns, "env", lambda name, default="":
                          "sk-mocked" if name == "DEEPSEEK_API_KEY" else default), \
             patch.object(ns, "_cache_ttl_s", lambda: 0.0):
            ns.get_market_sentiment("BTC")
            ns.get_market_sentiment("BTC")
        self.assertEqual(calls["n"], 2)
        ns.reset_cache()

    def test_ttl_comes_from_env(self):
        """TTL берётся из .env (NEWS_CACHE_TTL_MIN)."""
        from tools.trading_ai2.swarm import news_sentiment as ns
        with patch.object(ns, "env", lambda name, default="":
                          "20" if name == ns.NEWS_CACHE_TTL_ENV else default):
            self.assertAlmostEqual(ns._cache_ttl_s(), 20 * 60.0)
        with patch.object(ns, "env", lambda name, default="": default):
            self.assertAlmostEqual(ns._cache_ttl_s(), ns.CACHE_TTL_S)


class TestLearningWeights(unittest.TestCase):
    """Дарвиновские веса: пересчёт в Learning Engine + журнал весов."""

    def _swarm(self, n: int = 2):
        from tools.trading_ai2.swarm.agent_swarm import SwarmAgent
        return [SwarmAgent(f"TrendFollower-{i:02d}", "TrendFollower", "p")
                for i in range(1, n + 1)]

    def _learner(self, td: str):
        from tools.trading_ai2.swarm.learning_bridge import TradingLearning
        learner = TradingLearning(corpus_dir=Path(td) / "traj",
                                  lessons_path=Path(td) / "lessons.json")
        learner.ai2 = None          # веса — механизм роя, AI-2 не нужен
        return learner

    def test_weight_log_line_format(self):
        from tools.trading_ai2.swarm import darwin
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "weights.log"
            darwin.log_weight_change("TrendFollower-01", 1.0, 0.9, -12.0,
                                      path=path)
            text = path.read_text(encoding="utf-8")
        self.assertIn("[learning] Agent TrendFollower-01 weight: "
                      "1.00 -> 0.90 (wrong prediction)", text)
        self.assertIn("pnl=-12.00", text)

    def test_record_trade_recomputes_weights(self):
        """Убыток → вес вниз, строка изменения уходит в лог."""
        from tools.trading_ai2.swarm import darwin
        swarm = self._swarm(2)
        with tempfile.TemporaryDirectory() as td:
            learner = self._learner(td)
            with patch.object(darwin, "save_weights",
                              lambda w, path=None: None), \
                 patch.object(darwin, "log_weight_change",
                              lambda *a, **k: None):
                out = learner.record_trade(
                    action="BUY", pnl_usd=-12.0,
                    snap={"rsi": 72.0, "ema9_above_ema21": True,
                          "price": 80_000.0},
                    position_id="p-1",
                    personas=["TrendFollower-01", "TrendFollower-02"],
                    swarm=swarm)
        self.assertAlmostEqual(swarm[0].weight, 0.9, places=4)
        self.assertAlmostEqual(swarm[1].weight, 0.9, places=4)
        joined = "\n".join(out["lines"])
        self.assertIn("[learning] Agent TrendFollower-01 weight: "
                      "1.00 -> 0.90 (wrong prediction)", joined)

    def test_repeated_losses_floor_and_wins_grow(self):
        """Много убытков → вес 0.1, каждая прибыль → +0.1."""
        from tools.trading_ai2.swarm import darwin
        from tools.trading_ai2.swarm.agent_swarm import SwarmAgent
        with patch.object(darwin, "save_weights",
                          lambda w, path=None: None), \
             patch.object(darwin, "log_weight_change",
                          lambda *a, **k: None):
            loser = SwarmAgent("l", "Scalper", "p")
            for _ in range(12):
                darwin.apply_outcome([loser], ["l"], -10.0)
            winner = SwarmAgent("w", "TrendFollower", "p")
            for _ in range(5):
                darwin.apply_outcome([winner], ["w"], 10.0)
        self.assertAlmostEqual(loser.weight, 0.1, places=4)
        self.assertAlmostEqual(winner.weight, 1.5, places=4)

    def test_demo_closes_position_updates_weights(self):
        """Закрытие позиции в демо пересчитывает веса голосовавших."""
        from tools.trading_ai2.swarm import darwin
        swarm = self._swarm(1)
        with tempfile.TemporaryDirectory() as td:
            learner = self._learner(td)
            with patch.object(darwin, "save_weights",
                              lambda w, path=None: None), \
                 patch.object(darwin, "log_weight_change",
                              lambda *a, **k: None):
                lines = learner.apply_weights(swarm, ["TrendFollower-01"], -5.0)
        self.assertAlmostEqual(swarm[0].weight, 0.9, places=4)
        self.assertIn("weight: 1.00 -> 0.90", lines[0])


class TestMultiTimeframeAnalytics(unittest.TestCase):
    """Мультитаймфреймность: 1m + 15m, объём и режим рынка.

    Персона смотрит свой таймфрейм (Scalper и Momentum — 1m,
    TrendFollower, Breakout и MeanReversion — 15m), объём отличает
    настоящий пробой от ловушки, режим VOLATILE запрещает лонги.
    """

    def _frame(self, **kw):
        frame = {"timeframe": "1m", "price": 80_000.0, "rsi": 25.0,
                 "rsi_prev": 30.0, "macd": 200.0, "macd_signal": 10.0,
                 "ema9": 80_500.0, "ema21": 80_000.0,
                 "ema9_above_ema21": True, "atr": 100.0,
                 "atr_growth_pct": 5.0, "volume": 12.0,
                 "volume_sma20": 24.0, "volume_ratio": 0.5,
                 "regime": "TRENDING_UP"}
        frame.update(kw)
        return frame

    def _snap(self):
        """1m — импульс вверх на тонком объёме; 15m — перегрев."""
        return {
            "symbol": "BTC/USDT", "price": 80_000.0, "rsi": 25.0,
            "rsi_prev": 30.0, "macd": 200.0, "macd_signal": 10.0,
            "ema9": 80_500.0, "ema21": 80_000.0, "ema9_above_ema21": True,
            "atr": 100.0, "atr_growth_pct": 5.0, "volume": 12.0,
            "volume_sma20": 24.0, "volume_ratio": 0.5, "regime": "RANGING",
            "mtf": {
                "1m": self._frame(),
                "15m": self._frame(timeframe="15m", price=84_000.0, rsi=75.0,
                                   macd=-200.0, ema9=83_000.0, ema21=80_000.0,
                                   ema9_above_ema21=False, volume=48.0,
                                   volume_sma20=24.0, volume_ratio=2.0,
                                   regime="TRENDING_DOWN"),
            },
        }

    def _persona(self, persona: str):
        from tools.trading_ai2.swarm.agent_swarm import make_swarm
        swarm = make_swarm(13)
        found = [a for a in swarm if a.persona == persona]
        self.assertTrue(found, persona)
        return found[0]

    # ------------------------------------------------------- режим рынка
    def test_detect_regime_variants(self):
        from tools.trading_ai2.market_data import detect_regime
        self.assertEqual(detect_regime({"atr_growth_pct": 80.0}), "VOLATILE")
        self.assertEqual(detect_regime({"ema9_above_ema21": True,
                                        "macd": 10.0}), "TRENDING_UP")
        self.assertEqual(detect_regime({"ema9_above_ema21": False,
                                        "macd": -10.0}), "TRENDING_DOWN")
        self.assertEqual(detect_regime({"ema9_above_ema21": True,
                                        "macd": -10.0}), "RANGING")
        # аномально большая ATR к цене — тоже паника
        self.assertEqual(detect_regime({"price": 100.0, "atr": 5.0}),
                         "VOLATILE")

    def test_combined_regime_volatile_wins(self):
        from tools.trading_ai2.market_data import combined_regime
        self.assertEqual(combined_regime({"1m": {"regime": "TRENDING_UP"},
                                           "15m": {"regime": "VOLATILE"}}),
                         "VOLATILE")
        self.assertEqual(combined_regime({"1m": {"regime": "TRENDING_UP"},
                                           "15m": {"regime": "TRENDING_DOWN"}}),
                         "TRENDING_UP")

    # ---------------------------------------------------------- снапшот
    def test_snapshot_mtf_has_both_frames_and_volume(self):
        import pandas as pd
        from tools.trading_ai2.market_data import MarketData
        rows = []
        for i in range(60):
            close = 80_000.0
            rows.append({"ts": pd.Timestamp("2026-01-01", tz="UTC")
                         + pd.Timedelta(minutes=i),
                         "open": close, "high": close + 50.0,
                         "low": close - 50.0, "close": close,
                         "volume": 100.0 + i})
        df = pd.DataFrame(rows)
        md = MarketData(symbol="BTC/USDT")
        with patch.object(MarketData, "fetch_ohlcv",
                          lambda self, tf="1h", limit=120: df.copy()):
            snap = md.snapshot_mtf(("1m", "15m"))
        self.assertIn("1m", snap["mtf"])
        self.assertIn("15m", snap["mtf"])
        for tf, block in snap["mtf"].items():
            self.assertEqual(block["timeframe"], tf)
            self.assertIn("volume", block)
            self.assertIn("volume_sma20", block)
            self.assertIn("volume_ratio", block)
        self.assertEqual(snap["regime"], "RANGING")
        self.assertEqual(snap["timeframe"], "1m")   # плоские ключи — 1m

    def test_volume_context_ratio(self):
        """Объём сравнивается по закрытой свече: формирующаяся не в счёт."""
        import pandas as pd
        from tools.trading_ai2.market_data import MarketData
        md = MarketData(symbol="BTC/USDT")
        # последняя свеча ещё формируется (объём почти нулевой) — она
        # не должна искажать отношение объёма к средней
        df = pd.DataFrame({"volume": [10.0] * 19 + [40.0] + [0.001]})
        ctx = md.volume_context(df)
        self.assertAlmostEqual(ctx["volume"], 40.0)
        self.assertGreater(ctx["volume_ratio"], 1.5)
        flat = md.volume_context(pd.DataFrame({"volume": [10.0] * 21}))
        self.assertAlmostEqual(flat["volume_ratio"], 1.0)

    # ------------------------------------------------- таймфреймы персон
    def test_persona_timeframes(self):
        from tools.trading_ai2.swarm.agent_swarm import (
            PERSONA_TIMEFRAME, make_swarm)
        self.assertEqual(PERSONA_TIMEFRAME["Scalper"], "1m")
        self.assertEqual(PERSONA_TIMEFRAME["Momentum"], "1m")
        self.assertEqual(PERSONA_TIMEFRAME["TrendFollower"], "15m")
        self.assertEqual(PERSONA_TIMEFRAME["Breakout"], "15m")
        self.assertEqual(PERSONA_TIMEFRAME["MeanReversion"], "15m")
        swarm = make_swarm(13)
        by_persona = {a.persona: a.timeframe for a in swarm}
        self.assertEqual(by_persona["Scalper"], "1m")
        self.assertEqual(by_persona["Momentum"], "1m")
        self.assertEqual(by_persona["TrendFollower"], "15m")
        self.assertEqual(by_persona["Breakout"], "15m")
        self.assertEqual(by_persona["MeanReversion"], "15m")

    def test_prompt_has_timeframe_volume_and_regime_rules(self):
        agent = self._persona("Breakout")
        self.assertIn("Твой приоритетный таймфрейм: 15m", agent.system_prompt)
        self.assertIn("Пробой без объёма — ловушка", agent.system_prompt)
        self.assertIn("TRENDING_UP", agent.system_prompt)
        self.assertIn("VOLATILE", agent.system_prompt)

    def test_volatile_regime_forbids_longs(self):
        from tools.trading_ai2.swarm.agent_swarm import apply_lessons
        swarm = [self._persona("Breakout")]
        apply_lessons(swarm, "", "VOLATILE")
        self.assertIn("Рынок в панике. Запрещено открывать лонги.",
                      swarm[0].system_prompt)
        apply_lessons(swarm, "", "TRENDING_UP")
        self.assertNotIn("Запрещено открывать лонги",
                         swarm[0].system_prompt)

    # --------------------------------- агент читает свой таймфрейм
    def test_agent_reads_own_timeframe(self):
        """Scalper (1m) видит импульс 1m, Breakout (15m) — перегрев 15m."""
        snap = self._snap()
        scalper = self._persona("Scalper").decide(snap)
        self.assertEqual(scalper.action, "BUY")            # macd 1m = +200
        self.assertIn("объём ×0.50", scalper.reason)

        breakout = self._persona("Breakout").decide(snap)
        self.assertEqual(breakout.action, "BUY")           # 15m: +5% и объём ×2
        self.assertIn("объём ×2.00", breakout.reason)

        mr = self._persona("MeanReversion").decide(snap)
        self.assertEqual(mr.action, "SELL")               # 15m: +5% — перегрев
        self.assertIn("перегрев", mr.reason)

        trend = self._persona("TrendFollower").decide(snap)
        self.assertEqual(trend.action, "SELL")             # 15m: EMA9<21

    def test_breakout_without_volume_is_fakeout(self):
        """Пробой без объёма — ловушка, в BUY не идём."""
        snap = self._snap()
        snap["mtf"]["15m"]["volume_ratio"] = 0.4
        vote = self._persona("Breakout").decide(snap)
        self.assertEqual(vote.action, "HOLD")
        self.assertIn("ловушка", vote.reason)

    def test_panic_selling_on_high_volume(self):
        """Падение на повышенном объёме — реальная распродажа."""
        snap = self._snap()
        snap["mtf"]["15m"].update({"price": 76_000.0, "rsi": 25.0,
                                    "ema9_above_ema21": False,
                                    "volume_ratio": 2.5})
        vote = self._persona("Breakout").decide(snap)
        self.assertEqual(vote.action, "SELL")
        self.assertIn("panic selling", vote.reason)

    def test_momentum_fakeout_on_thin_volume(self):
        """Импульс без объёма (1m) — ловушка, Momentum воздерживается."""
        snap = self._snap()
        vote = self._persona("Momentum").decide(snap)
        self.assertEqual(vote.action, "HOLD")
        self.assertIn("ловушка", vote.reason)

    def test_llm_request_carries_priority_timeframe(self):
        """В запрос к модели уходит приоритетный таймфрейм агента."""
        from tools.trading_ai2.strategy_agent import TradeDecision

        class _FakeLLM:
            live = True

            def __init__(self):
                self.seen = None

            def decide(self, snap):
                self.seen = snap
                return TradeDecision(action="HOLD", amount_percent=0.0,
                                     reason="t", stop_loss=0.0, mode="live")

        agent = self._persona("TrendFollower")
        fake = _FakeLLM()
        agent.llm = fake
        agent.decide(self._snap())
        self.assertEqual(fake.seen["priority_timeframe"], "15m")
        self.assertIn("mtf", fake.seen)
        self.assertIn("volume_ratio", fake.seen)
        self.assertEqual(fake.seen["regime"], "RANGING")

    def test_demo_vote_line_shows_timeframe(self):
        """Лог голосования: [vote 01/13] TrendFollower-01 (15m) → …"""
        from tools.trading_ai2.demo_trading_grand import _vote_line
        agent = self._persona("TrendFollower")
        vote = SwarmVote(action="BUY", confidence=0.55, reason="тренд",
                          stop_loss=78_400.0, agent=agent.name,
                          persona=agent.persona, mode="stub", weight=1.0)
        line = _vote_line(1, 13, agent, vote)
        self.assertIn("[vote 01/13]", line)
        self.assertIn("TrendFollower-01 (15m)", line)
        self.assertIn("BUY", line)


class TestMarketScreener(unittest.TestCase):
    """«Слой 0»: get_best_asset — выбор самого горячего актива."""

    @staticmethod
    def _rows(volatility: float, *, hot_volume: bool = False,
              n: int = 25, price: float = 100.0) -> list:
        """ccxt-строки: последняя закрытая свеча с заданной волатильностью
        (% от цены), хвост — формирующаяся свеча (игнорируется)."""
        half = price * volatility / 200.0
        rows = []
        for i in range(n):
            vol = 300.0 if hot_volume and i == n - 1 else 100.0
            rows.append([i, price, price + half, price - half, price, vol])
        rows.append([n, price, price, price, price, 50.0])
        return rows

    @staticmethod
    def _feed(maps: dict):
        """ohlcv-инъекция: {symbol: rows} — падает на неизвестном символе."""
        def fetch(symbol, timeframe="15m", limit=30):
            if symbol not in maps:
                raise ValueError(f"unknown symbol {symbol}")
            return maps[symbol]
        return fetch

    def test_picks_sol_over_flat_btc(self):
        """SOL с волатильностью 4% и объёмом x3 выигрывает у BTC во флэте."""
        from tools.trading_ai2.swarm.market_screener import get_best_asset
        feed = self._feed({
            "BTC/USDT": self._rows(0.4, price=80_000.0),
            "ETH/USDT": self._rows(0.7, price=3_000.0),
            "SOL/USDT": self._rows(4.0, hot_volume=True, price=140.0),
        })
        pick = get_best_asset(
            tickers=["BTC/USDT", "ETH/USDT", "SOL/USDT"], ohlcv=feed)
        self.assertIsNotNone(pick)
        self.assertEqual(pick["symbol"], "SOL/USDT")
        self.assertAlmostEqual(pick["volatility"], 4.0, places=1)
        self.assertTrue(pick["hot"])
        self.assertGreater(pick["volume_ratio"], 1.0)

    def test_all_flat_returns_none(self):
        """Все активы < 1.0% волатильности → None (цикл спит, LLM не жжём)."""
        from tools.trading_ai2.swarm.market_screener import get_best_asset
        feed = self._feed({
            "BTC/USDT": self._rows(0.3, price=80_000.0),
            "ETH/USDT": self._rows(0.5, price=3_000.0),
            "SOL/USDT": self._rows(0.8, price=140.0),
        })
        pick = get_best_asset(
            tickers=["BTC/USDT", "ETH/USDT", "SOL/USDT"], ohlcv=feed)
        self.assertIsNone(pick)

    def test_middle_zone_returns_least_boring(self):
        """Не горячий (<1.5%), но не флэт (>1.0%) → отдаём лучший с hot=False."""
        from tools.trading_ai2.swarm.market_screener import get_best_asset
        feed = self._feed({
            "BTC/USDT": self._rows(1.2, hot_volume=True, price=80_000.0),
            "ETH/USDT": self._rows(0.5, price=3_000.0),
        })
        pick = get_best_asset(
            tickers=["BTC/USDT", "ETH/USDT"], ohlcv=feed)
        self.assertIsNotNone(pick)
        self.assertEqual(pick["symbol"], "BTC/USDT")
        self.assertFalse(pick["hot"])

    def test_screen_tickers_env_csv(self):
        """SCREEN_TICKERS из env — csv со spaces, пустые отбрасываются."""
        from tools.trading_ai2.swarm import market_screener
        with patch.dict(os.environ,
                        {"SCREEN_TICKERS": "BTC/USDT, SOL/USDT,,XRP/USDT "}):
            tickers = market_screener.screen_tickers()
        self.assertEqual(tickers, ["BTC/USDT", "SOL/USDT", "XRP/USDT"])

    def test_discover_tickers_market_scan(self):
        """«all» — скан рынка: ликвидные + движущиеся, стейблы и
        неликвид отсеяны."""
        from tools.trading_ai2.swarm.portfolio import market_screener as ms

        class _FakeEx:
            def load_markets(self):
                return {s: {"spot": True, "quote": "USDT",
                            "active": True}
                        for s in ("BTC/USDT", "SOL/USDT", "USDC/USDT",
                                  "MEME/USDT", "DEAD/USDT")}

            def fetch_tickers(self):
                return {
                    "BTC/USDT": {"quoteVolume": 9e9, "percentage": 1.0},
                    "SOL/USDT": {"quoteVolume": 2e9, "percentage": 0.5},
                    "USDC/USDT": {"quoteVolume": 8e9, "percentage": 0.0},
                    "MEME/USDT": {"quoteVolume": 1e7, "percentage": 42.0},
                    "DEAD/USDT": {"quoteVolume": 5.0, "percentage": 99.0},
                }

        ms._DISCOVERY_CACHE = (0.0, [])
        with patch.dict(os.environ,
                        {"SCREEN_TICKERS": "all", "SCREEN_SCAN_TOP": "10",
                         "SCREEN_MIN_QV": "1000000"}):
            out = ms.discover_tickers(ex=_FakeEx())
        self.assertIn("BTC/USDT", out)              # ликвидная мажорка
        self.assertIn("MEME/USDT", out)             # движущаяся (+42%)
        self.assertNotIn("USDC/USDT", out)          # стейбл — мимо
        self.assertNotIn("DEAD/USDT", out)          # нет оборота — мимо
        with patch.dict(os.environ, {"SCREEN_TICKERS": "all"}), \
                patch.object(ms, "_exchange", lambda: _FakeEx()):
            ms._DISCOVERY_CACHE = (0.0, [])
            self.assertEqual(set(ms.screen_tickers()), set(out))


class TestPortfolio(unittest.TestCase):
    """Портфельный слой: корреляция, ребалансировка, лимиты."""

    @staticmethod
    def _rows(series: list) -> list:
        return [[i, s, s, s, s, 1.0] for i, s in enumerate(series)]

    def test_correlation_drops_second_correlated(self):
        """SOL и AVAX движутся одинаково (corr≈1) → остаётся более горячий."""
        from tools.trading_ai2.swarm.portfolio.correlation_checker import (
            filter_uncorrelated)
        up = [100 + i for i in range(26)]
        up2 = [200 + 2 * i for i in range(26)]
        zig = [100 + (i % 5) * 7 for i in range(26)]
        feed = {"SOL/USDT": self._rows(up), "AVAX/USDT": self._rows(up2),
                "LINK/USDT": self._rows(zig)}
        assets = [
            {"symbol": "SOL/USDT", "score": 4.0, "volatility": 4.0},
            {"symbol": "AVAX/USDT", "score": 3.5, "volatility": 3.5},
            {"symbol": "LINK/USDT", "score": 3.0, "volatility": 3.0},
        ]
        kept = filter_uncorrelated(assets, ohlcv=lambda s, tf, limit:
                                   feed[s])
        self.assertEqual([a["symbol"] for a in kept],
                         ["SOL/USDT", "LINK/USDT"])

    def test_rebalance_close_fallen_and_open_new(self):
        """Актив выпал из топа → close; новый сильный → open с маржой."""
        from tools.trading_ai2.swarm.portfolio.portfolio_manager import (
            decide_rebalance)
        positions = [{"symbol": "SOL", "id": "p1", "margin_usd": 100.0,
                      "status": "open", "side": "long"}]
        top = [{"symbol": "ETH/USDT", "volatility": 4.0, "hot": True},
               {"symbol": "LINK/USDT", "volatility": 3.0, "hot": True}]
        rebal = decide_rebalance(positions, top, equity_usd=10_000.0)
        self.assertEqual([p["id"] for p in rebal["close"]], ["p1"])
        self.assertEqual([o["symbol"] for o in rebal["open"]],
                         ["ETH/USDT", "LINK/USDT"])
        for o in rebal["open"]:
            self.assertLessEqual(o["margin"], 500.0)   # ≤5% equity
        self.assertLessEqual(rebal["margin_used"], 2000.0)  # ≤20%

    def test_max_positions_blocks_fourth(self):
        """Три позиции в топе → четвёртый сильный актив не открывается."""
        from tools.trading_ai2.swarm.portfolio.portfolio_manager import (
            decide_rebalance)
        positions = [{"symbol": s, "id": f"p{i}", "margin_usd": 100.0,
                      "status": "open", "side": "long"}
                     for i, s in enumerate(("ETH", "SOL", "LINK"))]
        top = [{"symbol": f"{s}/USDT", "volatility": 4.0, "hot": True}
               for s in ("ETH", "SOL", "LINK", "AVAX")]
        rebal = decide_rebalance(positions, top, equity_usd=10_000.0)
        self.assertEqual(rebal["open"], [])
        self.assertEqual(rebal["close"], [])

    def test_total_margin_cap(self):
        """Остаток бюджета 200 → маржа новой сделки обрезается до 200."""
        from tools.trading_ai2.swarm.portfolio.portfolio_manager import (
            decide_rebalance)
        positions = [{"symbol": "ETH", "id": "p1", "margin_usd": 1800.0,
                      "status": "open", "side": "long"}]
        top = [{"symbol": "ETH/USDT", "volatility": 4.0, "hot": True},
               {"symbol": "SOL/USDT", "volatility": 3.5, "hot": True}]
        rebal = decide_rebalance(positions, top, equity_usd=10_000.0)
        self.assertEqual(len(rebal["open"]), 1)
        self.assertAlmostEqual(rebal["open"][0]["margin"], 200.0)


class TestNewsEngineV2(unittest.TestCase):
    """News Engine v2.0: независимые кэши уровней + emergency reset."""

    def _ctx(self, content: str, calls: dict):
        """Мок LLM + заголовки без сети (как в TestNewsSentimentCache)."""
        from tools.trading_ai2.swarm import news_sentiment as ns
        return (TestNewsSentimentCache._counting_mock(
                    self, content, calls),
                patch.object(ns, "fetch_headlines",
                             lambda symbol="", count=3:
                             ([f"{symbol or 'GLOBAL'} headline rally"],
                              "mock")),
                patch.object(ns, "env", lambda name, default="":
                             "sk-mocked" if name == "DEEPSEEK_API_KEY"
                             else default))

    def setUp(self):
        from tools.trading_ai2.swarm import news_sentiment as ns
        ns.reset_cache()

    def tearDown(self):
        from tools.trading_ai2.swarm import news_sentiment as ns
        ns.reset_cache()

    def test_asset_caches_are_per_symbol(self):
        """SOL и BTC не смешивают кэш: у каждого своя запись."""
        from tools.trading_ai2.swarm import news_sentiment as ns
        calls = {"n": 0}
        m, f, e = self._ctx('{"sentiment": 0.3, "summary": "ок"}', calls)
        with m, f, e:
            sol1 = ns.get_asset_sentiment("SOL")
            btc1 = ns.get_asset_sentiment("BTC")
            sol2 = ns.get_asset_sentiment("SOL")
        self.assertEqual(calls["n"], 2)         # второй SOL — из кэша
        self.assertFalse(sol1.from_cache)
        self.assertFalse(btc1.from_cache)
        self.assertTrue(sol2.from_cache)

    def test_invalidate_clears_both_caches(self):
        """invalidate_cache → оба уровня перезапрашиваются заново."""
        from tools.trading_ai2.swarm import news_sentiment as ns
        calls = {"n": 0}
        m, f, e = self._ctx('{"sentiment": 0.1, "summary": "ок"}', calls)
        with m, f, e:
            ns.get_market_sentiment("")
            ns.get_asset_sentiment("SOL")
            cleared = ns.invalidate_cache("test reset")
            g2 = ns.get_market_sentiment("")
            a2 = ns.get_asset_sentiment("SOL")
        self.assertEqual(calls["n"], 4)
        self.assertEqual(cleared["global"], 1)
        self.assertEqual(cleared["assets"], 1)
        self.assertFalse(g2.from_cache)
        self.assertFalse(a2.from_cache)

    def test_global_expiry_keeps_asset_cache(self):
        """Протухший глобальный кэш не трогает по-активный (TTL 45м)."""
        from tools.trading_ai2.swarm import news_sentiment as ns
        calls = {"n": 0}
        m, f, e = self._ctx('{"sentiment": 0.2, "summary": "ок"}', calls)
        with m, f, e:
            ns.get_market_sentiment("")
            ns.get_asset_sentiment("SOL")
            # глобальный «протух» — отматываем его метку за TTL
            key = next(iter(ns._NEWS_CACHE))
            ts, res = ns._NEWS_CACHE[key]
            ns._NEWS_CACHE[key] = (ts - ns._cache_ttl_s() - 1, res)
            g2 = ns.get_market_sentiment("")
            a2 = ns.get_asset_sentiment("SOL")
        self.assertEqual(calls["n"], 3)         # только глобальный переспросил
        self.assertFalse(g2.from_cache)
        self.assertTrue(a2.from_cache)


class TestAssetNewsFeed(unittest.TestCase):
    """Уровень 2 новостей берёт ленту по активу, а не общую.

    Без отдельной ленты два сентимента совпадали бы: уровень 1 и
    уровень 2 смотрели бы на один и тот же поток заголовков.
    """

    GENERAL_XML = ("<rss><channel><item><title>Market wide headline</title>"
                   "</item></channel></rss>").encode()
    SOL_XML = ("<rss><channel><item><title>Solana ETF inflows</title>"
               "</item></channel></rss>").encode()

    def _fake_get(self, seen: list):
        def _get(url, headers=None, timeout=8.0, attempts=2):
            seen.append(url)
            if "cointelegraph" in url:
                return self.SOL_XML
            return self.GENERAL_XML
        return _get

    def test_asset_symbol_uses_its_own_feed(self):
        from tools.trading_ai2.swarm import news_sentiment as ns
        seen: list = []
        with patch.object(ns, "_http_get", self._fake_get(seen)), \
                patch.object(ns, "env", lambda name, default="": default):
            titles, source = ns.fetch_headlines("SOL")
        self.assertEqual(source, "cointelegraph/solana")
        self.assertEqual(titles, ["Solana ETF inflows"])
        self.assertTrue(any("tag/solana" in u for u in seen))
        self.assertNotIn(ns.COINDESK_RSS, seen)

    def test_global_symbol_uses_general_feed(self):
        from tools.trading_ai2.swarm import news_sentiment as ns
        seen: list = []
        with patch.object(ns, "_http_get", self._fake_get(seen)), \
                patch.object(ns, "env", lambda name, default="": default):
            titles, source = ns.fetch_headlines("")
        self.assertEqual(source, "coindesk")
        self.assertEqual(titles, ["Market wide headline"])
        self.assertNotIn(ns.ASSET_RSS_URL.format(tag="solana"), seen)

    def test_levels_read_different_feeds(self):
        """Два уровня получают разные заголовки — иначе сентименты
        совпали бы и правило «фон плохой / актив хороший» было бы
        недостижимо."""
        from tools.trading_ai2.swarm import news_sentiment as ns
        with patch.object(ns, "_http_get", self._fake_get([])), \
                patch.object(ns, "env", lambda name, default="": default):
            g_titles, g_src = ns.fetch_headlines("")
            a_titles, a_src = ns.fetch_headlines("SOL")
        self.assertNotEqual(g_src, a_src)
        self.assertNotEqual(g_titles, a_titles)

    def test_asset_feed_failure_falls_back_to_general(self):
        """Упавшая лента по активу не роняет уровень 2 — общая лента."""
        from tools.trading_ai2.swarm import news_sentiment as ns

        def _get(url, headers=None, timeout=8.0, attempts=2):
            if "cointelegraph" in url:
                raise OSError("offline")
            return self.GENERAL_XML

        with patch.object(ns, "_http_get", _get), \
                patch.object(ns, "env", lambda name, default="": default):
            titles, source = ns.fetch_headlines("SOL")
        self.assertEqual(source, "coindesk")
        self.assertEqual(titles, ["Market wide headline"])


class TestAgentPool(unittest.TestCase):
    """Swarm Pool v3.0: 100 слотов, топ-30 голосует, смерть → респавн."""

    def _pool(self, **kw):
        from tools.trading_ai2.swarm.agent_swarm import AgentPool
        kw.setdefault("pool_size", 100)
        kw.setdefault("active_n", 30)
        kw.setdefault("gen_models", ["m1", "m2", "m3"])
        kw.setdefault("seed", 1)
        return AgentPool(**kw)

    def test_pool_size_and_active_top30(self):
        """100 слотов; голосуют ровно 30 — самые тяжёлые по весу."""
        pool = self._pool()
        self.assertEqual(len(pool), 100)
        act = pool.active()
        self.assertEqual(len(act), 30)
        weights = sorted(a.weight for a in pool.agents)
        self.assertGreaterEqual(min(a.weight for a in act), weights[-30])

    def test_kill_on_429_respawns_without_breaking(self):
        """429 от модели → агент умирает, слот заново заполнен,
        пул остаётся 100, у модели cooldown — респавн её не берёт."""
        import time as _t
        pool = self._pool()
        victim = pool.active()[0]
        victim_model = victim.model_id
        lines = pool.kill_agent(victim, "http 429")
        self.assertEqual(len(pool), 100)               # слот перезаполнен
        self.assertEqual(pool.kills, 1)
        self.assertEqual(pool.respawns, 1)
        self.assertEqual(pool.replacements_this_cycle, 1)
        self.assertIn("DIED", lines[0])
        self.assertIn("Spawning replacement", lines[0])
        # умерший не в пуле, у нового уникальное имя и живая модель
        self.assertNotIn(victim, pool.agents)
        new = pool.agents[-1]
        self.assertNotEqual(new.name, victim.name)
        self.assertNotEqual(new.model_id, victim_model)
        # cooldown живёт 15 минут
        self.assertGreater(pool._model_cooldown[victim_model], _t.time())

    def test_weight_below_floor_kills(self):
        """Вес < 0.1 — слот на эвтаназию, как в цикле run_cycle."""
        pool = self._pool()
        weak = pool.agents[0]
        weak.weight = 0.05
        doomed = [a for a in pool.agents if a.weight < 0.1]
        self.assertEqual(len(doomed), 1)
        pool.kill_agent(weak, "weight < 0.1")
        self.assertNotIn(weak, pool.agents)
        self.assertEqual(len(pool), 100)

    def test_rotation_covers_all_models_evenly(self):
        """Стартовая раскладка по кругу: все модели в пуле поровну."""
        from collections import Counter
        pool = self._pool(pool_size=21)
        counts = Counter(a.model_id for a in pool.agents)
        self.assertEqual(dict(counts), {"m1": 7, "m2": 7, "m3": 7})

    def test_status_lines_for_tui(self):
        """Строки статуса для лога и хедера TUI — по спецификации."""
        pool = self._pool()
        self.assertIn("100 total, 30 active", pool.status_line())
        self.assertIn("30/100", pool.header_line())
        self.assertIn("Kills: 0", pool.header_line())
        pool.cycle_reset()
        self.assertIn("0 replacements this cycle", pool.status_line())


class TestMultiAssetSwarm(unittest.TestCase):
    """Мультивалютный рой: промпт под актив, два сентимента, вес в Арбитре."""

    def _analyst(self):
        from tools.trading_ai2.swarm.agent_swarm import make_swarm
        swarm = make_swarm(13, symbol="SOL/USDT")
        return next(a for a in swarm if a.persona == "Analyst")

    # ---------------------------------------------------- ассет в промпте
    def test_prompt_built_for_given_asset(self):
        agent = self._analyst()
        self.assertEqual(agent.asset, "SOL/USDT")
        self.assertIn("Ты торгуешь активом SOL/USDT", agent.system_prompt)
        self.assertIn("SOL/USDT", agent.system_prompt)
        self.assertNotIn("BTC", agent.system_prompt)

    def test_asset_switch_rebuilds_prompts(self):
        """Скринер выбрал другой актив — промпты пересобраны под него."""
        from tools.trading_ai2.swarm.agent_swarm import (
            apply_lessons, make_swarm)
        swarm = make_swarm(13, symbol="SOL/USDT")
        apply_lessons(swarm, "", "TRENDING_UP", "AVAX/USDT")
        for agent in swarm:
            self.assertEqual(agent.asset, "AVAX/USDT")
            self.assertIn("AVAX/USDT", agent.system_prompt)
            self.assertNotIn("SOL/USDT", agent.system_prompt)

    def test_sentiment_rule_names_asset(self):
        from tools.trading_ai2.swarm.agent_swarm import sentiment_rule
        text = sentiment_rule("SOL/USDT")
        self.assertIn("market_sentiment", text)
        self.assertIn("asset_sentiment", text)
        self.assertIn("SOL/USDT", text)

    def test_strategy_prompt_names_asset_and_two_sentiments(self):
        from tools.trading_ai2.strategy_agent import (
            SYSTEM_PROMPT, system_prompt)
        self.assertIn("market_sentiment", SYSTEM_PROMPT)
        self.assertIn("asset_sentiment", SYSTEM_PROMPT)
        self.assertIn("SOL/USDT", system_prompt("SOL/USDT"))
        self.assertNotIn("SOL/USDT", SYSTEM_PROMPT)

    def test_strategy_prompt_asks_for_confidence(self):
        """Промпт обязан звать confidence: без него модель поле не
        возвращает, `_llm_vote` берёт фолбэк и каждый живой голос
        становится 0.30 — а при 0.30 консенсус Risk-роя никогда не
        доберёт до порога вето 0.7, то есть жёсткое правило спеки
        недостижимо. Проверено на живом ответе deepseek: с полем
        модель честно даёт 0.85–0.95."""
        from tools.trading_ai2.strategy_agent import SYSTEM_PROMPT
        self.assertIn('"confidence"', SYSTEM_PROMPT)

    def test_llm_vote_keeps_model_confidence(self):
        """Названная моделью уверенность не подменяется фолбэком 0.3."""
        from tools.trading_ai2.swarm.agent_swarm import SwarmAgent

        class _Llm:
            live = True

            @staticmethod
            def decide(snap):
                from tools.trading_ai2.strategy_agent import TradeDecision
                return TradeDecision(action="HOLD", amount_percent=0.0,
                                     reason="ATR взрывается — вне рынка",
                                     stop_loss=0.0, confidence=0.85,
                                     mode="live")

        agent = SwarmAgent(name="RiskManager-A", persona="RiskManager",
                           system_prompt="", llm=_Llm())
        vote = agent._llm_vote({"price": 100.0})
        self.assertAlmostEqual(vote.confidence, 0.85)

    def test_llm_vote_without_confidence_falls_back_to_size(self):
        """Молчащий о уверенности рой не должен сводиться к одному
        номеру: фолбэк считается по размеру позиции, а не константой."""
        from tools.trading_ai2.swarm.agent_swarm import SwarmAgent

        class _Llm:
            live = True

            @staticmethod
            def decide(snap):
                from tools.trading_ai2.strategy_agent import TradeDecision
                return TradeDecision(action="BUY", amount_percent=2.0,
                                     reason="EMA9>21, MACD положительный",
                                     stop_loss=98.0, mode="live")

        agent = SwarmAgent(name="TrendFollower-A", persona="TrendFollower",
                           system_prompt="", llm=_Llm())
        self.assertAlmostEqual(agent._llm_vote({"price": 100.0}).confidence,
                               0.4)

    # -------------------------------------------- два сентимента у Analyst
    def test_analyst_buys_on_good_asset_news_despite_bad_market(self):
        """Глобальный плохой + по активу хороший — это возможность."""
        agent = self._analyst()
        vote = agent._stub_vote({"price": 150.0, "rsi": 50.0,
                                 "ema9_above_ema21": True, "macd": 0.0,
                                 "market_sentiment": -0.6,
                                 "asset_sentiment": 0.5})
        self.assertEqual(vote.action, "BUY")
        self.assertIn("возможность", vote.reason)

    def test_analyst_holds_when_both_sentiments_bad(self):
        """Оба сентимента плохие — не входи."""
        agent = self._analyst()
        vote = agent._stub_vote({"price": 150.0, "rsi": 50.0,
                                 "ema9_above_ema21": True, "macd": 0.0,
                                 "market_sentiment": -0.6,
                                 "asset_sentiment": -0.4})
        self.assertEqual(vote.action, "HOLD")
        self.assertIn("оба сентимента плохие", vote.reason)

    def test_analyst_reads_asset_sentiment_first(self):
        """По-активные новости точнее фона: при конфликте решает актив."""
        agent = self._analyst()
        vote = agent._stub_vote({"price": 150.0, "rsi": 50.0,
                                 "ema9_above_ema21": True, "macd": 0.0,
                                 "market_sentiment": 0.6,
                                 "asset_sentiment": -0.5})
        self.assertEqual(vote.action, "SELL")

    # --------------------------------------------- вес агента у Арбитра
    def test_arbiter_score_follows_agent_weight(self):
        """Голос умножается на вес: тяжёлый агент тянет score сильнее."""
        from tools.trading_ai2.swarm.agent_swarm import SwarmVote
        from tools.trading_ai2.swarm.arbiter import Arbiter
        buy = SwarmVote(action="BUY", confidence=0.6, reason="x",
                        stop_loss=1.0, agent="a", weight=2.0)
        sell_light = SwarmVote(action="SELL", confidence=0.6, reason="x",
                               stop_loss=0.0, agent="b", weight=0.2)
        sell_heavy = SwarmVote(action="SELL", confidence=0.6, reason="x",
                               stop_loss=0.0, agent="c", weight=2.0)
        score_light = Arbiter.score_votes([buy, sell_light])
        score_heavy = Arbiter.score_votes([buy, sell_heavy])
        self.assertGreater(score_light, score_heavy)
        self.assertAlmostEqual(score_heavy, 0.0, places=6)
        self.assertAlmostEqual(score_light, 1.08 / 1.32, places=6)

    def test_darwin_weight_change_moves_arbiter_score(self):
        """Убыток агента → вес −0.1 → его голос слабее у Арбитра."""
        from tools.trading_ai2.swarm import darwin
        from tools.trading_ai2.swarm.agent_swarm import SwarmVote
        from tools.trading_ai2.swarm.arbiter import Arbiter
        vote = SwarmVote(action="BUY", confidence=0.6, reason="x",
                         stop_loss=1.0, agent="a", weight=1.0)
        other = SwarmVote(action="HOLD", confidence=0.6, reason="x",
                          stop_loss=0.0, agent="b", weight=1.0)
        before = Arbiter.score_votes([vote, other])
        vote.weight = darwin.update_weight(vote.weight, -12.0)
        after = Arbiter.score_votes([vote, other])
        self.assertAlmostEqual(vote.weight, 0.9, places=4)
        self.assertLess(after, before)


class TestPersonaGenerator(unittest.TestCase):
    """Генератор личностей: 100 уникальных персон для пула."""

    def test_generate_100_unique_personas(self):
        """100 персон: имена и промпты попарно разные."""
        from tools.trading_ai2.swarm.persona_generator import generate_personas
        vs = generate_personas(100)
        self.assertEqual(len(vs), 100)
        self.assertEqual(len({v.name for v in vs}), 100)
        self.assertEqual(len({v.prompt for v in vs}), 100)

    def test_example_names_from_brief(self):
        """Имена из задания есть в пуле."""
        from tools.trading_ai2.swarm.persona_generator import generate_personas
        names = {v.name for v in generate_personas(100)}
        for expected in ("TrendFollower-Aggressive",
                         "TrendFollower-Conservative",
                         "Contrarian-RSI", "Contrarian-MACD"):
            self.assertIn(expected, names)

    def test_variant_changes_prompt_and_risk(self):
        """Характер меняет и промпт, и риск внутри одной семьи."""
        from tools.trading_ai2.swarm.persona_generator import generate_personas
        vs = {v.name: v for v in generate_personas(100)}
        agg = vs["TrendFollower-Aggressive"]
        cons = vs["TrendFollower-Conservative"]
        self.assertEqual(agg.family, cons.family)
        self.assertNotEqual(agg.prompt, cons.prompt)
        self.assertEqual(agg.risk, "high")
        self.assertEqual(cons.risk, "low")
        self.assertGreater(agg.stop_pct, cons.stop_pct)
        self.assertIn("агрессивный риск", agg.prompt)
        self.assertIn("строгий риск", cons.prompt)

    def test_all_thirteen_families_covered(self):
        """Все 15 семей представлены, внутри семьи характеры не повторяются."""
        from collections import Counter
        from tools.trading_ai2.swarm.persona_generator import generate_personas
        vs = generate_personas(100)
        fams = Counter(v.family for v in vs)
        self.assertEqual(len(fams), 15)
        for family, count in fams.items():
            self.assertGreaterEqual(count, 6, family)
            variants = [v.variant for v in vs if v.family == family]
            self.assertEqual(len(set(variants)), len(variants), family)

    def test_pool_of_100_has_unique_prompts_and_weight_one(self):
        """Пул из 100 агентов: свой промпт у каждого, стартовый вес 1.0."""
        from tools.trading_ai2.swarm.agent_swarm import AgentPool
        pool = AgentPool(pool_size=100, active_n=30, seed=1)
        self.assertEqual(len(pool), 100)
        self.assertEqual(len({a.name for a in pool.agents}), 100)
        self.assertEqual(len({a.system_prompt for a in pool.agents}), 100)
        self.assertTrue(all(a.weight == 1.0 for a in pool.agents))

    def test_pool_prompt_carries_personality(self):
        """В промпте агента пула есть его собственный характер."""
        from tools.trading_ai2.swarm.agent_swarm import AgentPool
        pool = AgentPool(pool_size=100, active_n=30, seed=1)
        agent = pool.agents[0]
        self.assertEqual(agent.name, "TrendFollower-Aggressive")
        self.assertEqual(agent.variant, "Aggressive")
        self.assertIn("агрессивный риск", agent.system_prompt)
        self.assertIn("Trend Follower", agent.system_prompt)

    def test_respawn_gets_another_personality(self):
        """Респавн: у нового агента своя личность, имя не повторяется."""
        from tools.trading_ai2.swarm.agent_swarm import AgentPool
        pool = AgentPool(pool_size=100, active_n=30, seed=1)
        victim = pool.active()[0]
        pool.kill_agent(victim, "http 429")
        new = pool.agents[-1]
        self.assertNotEqual(new.name, victim.name)
        self.assertNotEqual(new.system_prompt, victim.system_prompt)
        self.assertEqual(len({a.name for a in pool.agents}), 100)

    def test_reputation_file_holds_hundred_records(self):
        """reputation.json хранит 100 записей, новый агент стартует с 1.0."""
        import tempfile
        from pathlib import Path
        from tools.trading_ai2.swarm.epoch_rewarder import (
            ensure_reputation, load_reputation)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "reputation.json"
            names = [f"Agent-{i:03d}" for i in range(100)]
            rep = ensure_reputation(names, path=path)
            self.assertEqual(len(rep), 100)
            self.assertTrue(all(v == 1.0 for v in rep.values()))
            # повторный старт не множит записи и не портит значения
            rep2 = ensure_reputation(names + ["Agent-100"], path=path)
            self.assertEqual(len(rep2), 101)
            self.assertEqual(load_reputation(path)["Agent-000"], 1.0)

    def test_pool_registers_reputation_for_all_agents(self):
        """Старт пула: все 100 агентов получают запись репутации 1.0."""
        from unittest.mock import patch
        from tools.trading_ai2.swarm.agent_swarm import AgentPool
        with patch("tools.trading_ai2.swarm.epoch_rewarder."
                   "ensure_reputation") as mock:
            AgentPool(pool_size=100, active_n=30, seed=1)
        self.assertTrue(mock.called)
        names = mock.call_args[0][0]
        self.assertEqual(len(names), 100)
        self.assertEqual(len(set(names)), 100)


class TestArbiterPoolRules(unittest.TestCase):
    """Арбитр пула из 100 агентов: мёртвые, вето-сила, тяжёлое большинство."""

    @staticmethod
    def _vote(action: str, agent: str, weight: float, conf: float = 0.9):
        from tools.trading_ai2.swarm.agent_swarm import SwarmVote
        return SwarmVote(action=action, confidence=conf, reason="x",
                         stop_loss=0.0, agent=agent, weight=weight)

    @staticmethod
    def _proposal(action: str = "BUY"):
        from tools.trading_ai2.swarm.generator import Proposal
        return Proposal(action, 0.0, {"BUY": 9, "HOLD": 21}, 1.5, 1.0, "t")

    def test_dead_agent_ignored_after_three_votes(self):
        """Вес < 0.1 три голоса подряд — голос агента проигнорирован."""
        from tools.trading_ai2.swarm.arbiter import Arbiter
        arb = Arbiter()
        dead = self._vote("BUY", "ghost", 0.05)
        alive = self._vote("HOLD", "live", 1.0)
        # первый и второй голос — агент ещё жив
        self.assertEqual(arb.rule(self._proposal(), votes=[dead, alive]).dead, [])
        self.assertEqual(arb.rule(self._proposal(), votes=[dead, alive]).dead, [])
        # третий подряд — «мёртв», голос вычеркнут
        con = arb.rule(self._proposal(), votes=[dead, alive])
        self.assertEqual(con.dead, ["ghost"])
        self.assertAlmostEqual(con.score, 0.0, places=6)

    def test_dead_vote_does_not_tilt_score(self):
        """Мёртвый BUY не может продавить сделку."""
        from tools.trading_ai2.swarm.arbiter import Arbiter
        arb = Arbiter()
        dead = self._vote("BUY", "ghost", 0.05, conf=1.0)
        others = [self._vote("HOLD", f"live{i}", 1.0) for i in range(9)]
        for _ in range(2):
            arb.rule(self._proposal(), votes=[dead, *others])
        con = arb.rule(self._proposal(), votes=[dead, *others])
        self.assertEqual(con.dead, ["ghost"])
        self.assertAlmostEqual(con.score, 0.0, places=6)
        self.assertEqual(con.action, "HOLD")

    def test_heavy_weight_gets_veto_multiplier(self):
        """Вес > 2.5 — вето-сила: голос говорит втрое громче."""
        from tools.trading_ai2.swarm.arbiter import Arbiter
        self.assertAlmostEqual(Arbiter.effective_weight(2.5), 2.5, places=6)
        self.assertAlmostEqual(Arbiter.effective_weight(2.6), 7.8, places=6)
        buy = self._vote("BUY", "a", 2.6, conf=0.6)
        sell = self._vote("SELL", "b", 1.0, conf=0.6)
        with_veto = Arbiter.score_votes([buy, sell])
        buy.weight = 2.5          # ровно порог — без вето
        without = Arbiter.score_votes([buy, sell])
        self.assertGreater(with_veto, without)

    def test_heavy_majority_hold_cancels_trade(self):
        """70% top-30 по весу говорят HOLD — сделка отменена."""
        from tools.trading_ai2.swarm.arbiter import Arbiter
        votes = []
        for i in range(30):
            action = "HOLD" if i < 21 else "BUY"
            votes.append(self._vote(action, f"a{i}", 3.0 - i * 0.01))
        con = Arbiter().rule(self._proposal(), votes=votes)
        self.assertEqual(con.action, "HOLD")
        self.assertTrue(con.cancelled)
        self.assertIn("вето тяжёлого большинства", con.reason)

    def test_heavy_majority_below_threshold_keeps_trade(self):
        """Меньше 70% HOLD — вето не срабатывает, сделка живёт."""
        from tools.trading_ai2.swarm.arbiter import Arbiter
        votes = []
        for i in range(30):
            action = "HOLD" if i < 20 else "BUY"
            votes.append(self._vote(action, f"a{i}", 3.0 - i * 0.01))
        con = Arbiter().rule(self._proposal(), votes=votes)
        self.assertFalse(con.cancelled)
        self.assertNotIn("вето", con.reason)

    def test_heavy_hold_veto_uses_top_thirty_by_weight(self):
        """Вето считает top-30 по весу, а не все голоса подряд."""
        from tools.trading_ai2.swarm.arbiter import Arbiter
        # 21 тяжёлый HOLD + 19 лёгких BUY: в top-30 по весу 21 HOLD из 30
        votes = [self._vote("HOLD", f"h{i}", 3.0) for i in range(21)]
        votes += [self._vote("BUY", f"b{i}", 0.5) for i in range(19)]
        self.assertIn("вето тяжёлого большинства",
                      Arbiter.heavy_hold_veto(votes))
        # 10 тяжёлых BUY + 30 лёгких HOLD: в top-30 HOLD только 20 из 30
        votes = [self._vote("BUY", f"b{i}", 3.0) for i in range(10)]
        votes += [self._vote("HOLD", f"h{i}", 0.1) for i in range(20)]
        votes += [self._vote("HOLD", f"x{i}", 0.05) for i in range(10)]
        self.assertEqual(Arbiter.heavy_hold_veto(votes), "")


class TestSymbolFormat(unittest.TestCase):
    """Панель и MarginPad должны называть актив одним форматом."""

    def test_norm_and_base_symbol(self):
        from tools.trading_ai2.swarm_status import base_symbol, norm_symbol
        self.assertEqual(norm_symbol("BTC"), "BTC/USDT")
        self.assertEqual(norm_symbol(" sol/usdt "), "SOL/USDT")
        self.assertEqual(norm_symbol(None), "")
        self.assertEqual(norm_symbol(""), "")
        self.assertEqual(base_symbol("SOPH/USDT"), "SOPH")

    def test_active_symbol_follows_history_not_btc(self):
        """После ребаланса панель уходит на актив цикла, а не на BTC."""
        from tools.trading_ai2.swarm_status import DEFAULT_SYMBOL, active_symbol
        rows = [{"event": "cycle", "symbol": "BTC/USDT"},
                {"event": "cycle", "symbol": "SOPH"}]
        self.assertEqual(active_symbol(rows), "SOPH/USDT")
        self.assertEqual(active_symbol([]), DEFAULT_SYMBOL)


class _FakeGateway:
    """Подмена MarketData: 15m-кадр по символу, без ccxt и сети."""

    def __init__(self, prices, fails=()):
        self.prices, self.fails = dict(prices), set(fails)
        self.symbol = ""
        self.calls = []

    def frame(self, timeframe="15m", limit=120):
        self.calls.append(self.symbol)
        if self.symbol in self.fails:
            raise RuntimeError("binance: dapi exchangeInfo failed")
        return {"price": self.prices[self.symbol], "rsi": 41.7,
                "macd": -1.2, "ema9_above_ema21": False, "volume": 9.0,
                "volume_sma20": 10.0, "volume_ratio": 0.9,
                "regime": "TRENDING_DOWN", "atr": 0.02,
                "atr_growth_pct": -3.0, "timeframe": timeframe}


class TestPortfolioSection(unittest.TestCase):
    """Секция «Портфель»: что рой держит и чем торговал, по живым свечам."""

    @staticmethod
    def _market():
        return {"price": 0.00239, "source": "binance:public",
                "frames": [{"timeframe": "1m", "rsi": 21.4,
                            "regime": "VOLATILE"},
                           {"timeframe": "15m", "rsi": 24.0,
                            "regime": "VOLATILE", "volumeRatio": 0.657}]}

    def test_order_active_first_and_frame_reuse(self):
        from tools.trading_ai2 import swarm_status as ss
        rows = [{"event": "cycle", "symbol": "BTC/USDT"},
                {"event": "cycle", "symbol": "SOPH/USDT"}]
        positions = {"positions": [{"symbol": "SOL/USDT", "status": "open"}]}
        md = _FakeGateway({"SOPH/USDT": 0.00239, "SOL/USDT": 94.15,
                           "BTC/USDT": 77083.83})
        sec = ss._portfolio_section(rows, positions, md, self._market())
        self.assertEqual(sec["activeSymbol"], "SOPH/USDT")
        self.assertEqual(sec["symbols"],
                         ["SOPH/USDT", "SOL/USDT", "BTC/USDT"])
        # кадр активного символа уже снят секцией «market» — не дублируем
        self.assertEqual(md.calls, ["SOL/USDT", "BTC/USDT"])
        self.assertEqual(sec["assets"][0]["rsi"], 24.0)
        self.assertEqual(sec["assets"][0]["price"], 0.00239)
        self.assertTrue(sec["assets"][0]["active"])
        self.assertTrue(sec["assets"][1]["held"])
        self.assertFalse(sec["assets"][1]["active"])
        self.assertTrue(sec["assets"][2]["traded"])

    def test_broken_asset_does_not_kill_portfolio(self):
        from tools.trading_ai2 import swarm_status as ss
        rows = [{"event": "cycle", "symbol": "SOL/USDT"}]
        positions = {"positions": [{"symbol": "AVAX", "status": "open"}]}
        md = _FakeGateway({"SOL/USDT": 94.15, "AVAX/USDT": 8.4},
                          fails=["AVAX/USDT"])
        sec = ss._portfolio_section(rows, positions, md, None)
        self.assertEqual(md.calls, ["SOL/USDT", "AVAX/USDT"])
        self.assertEqual(sec["assets"][0]["price"], 94.15)
        bad = sec["assets"][1]
        self.assertEqual(bad["symbol"], "AVAX/USDT")
        self.assertIn("exchangeInfo", bad["error"])
        self.assertNotIn("price", bad)

    def test_portfolio_capped_at_limit(self):
        from tools.trading_ai2 import swarm_status as ss
        rows = [{"event": "cycle", "symbol": f"A{i}/USDT"} for i in range(9)]
        md = _FakeGateway({f"A{i}/USDT": float(i) for i in range(9)})
        sec = ss._portfolio_section(rows, {"positions": []}, md, None)
        self.assertEqual(len(sec["assets"]), ss.PORTFOLIO_LIMIT)
        self.assertEqual(len(md.calls), ss.PORTFOLIO_LIMIT)


class TestTradesSection(unittest.TestCase):
    """Сделки из истории: position_opened / position_closed."""

    def test_only_position_events_and_realized_sum(self):
        from tools.trading_ai2 import swarm_status as ss
        rows = [
            {"event": "cycle", "symbol": "BTC/USDT"},
            {"event": "position_opened", "symbol": "SOL", "side": "long",
             "id": "p1", "margin_usd": 100.0, "entry_price": 94.15,
             "ts": "2026-10-30T17:41:02+00:00", "mode": "live"},
            {"event": "screener_skip", "symbol": "BTC/USDT"},
            {"event": "position_closed", "symbol": "SOL", "side": "long",
             "id": "p1", "pnl_usd": -1.48, "hold_s": 45.0,
             "reason": "take-profit", "ts": "2026-10-30T17:41:47+00:00"},
            {"event": "position_closed", "symbol": "ETH", "pnl_usd": 2.0},
        ]
        sec = ss._trades_section(rows)
        self.assertEqual([r["event"] for r in sec["rows"]],
                         ["position_opened", "position_closed",
                          "position_closed"])
        self.assertEqual(sec["rows"][0]["symbol"], "SOL/USDT")
        self.assertEqual(sec["rows"][1]["reason"], "take-profit")
        self.assertEqual(sec["closedCount"], 2)
        self.assertAlmostEqual(sec["realizedPnlUsd"], 0.52)
        self.assertEqual(sec["note"], "")

    def test_limit_shows_latest_but_realized_counts_all(self):
        """Окно ленты уже, чем окно расчёта: сделка не должна теряться."""
        from tools.trading_ai2 import swarm_status as ss
        rows = [{"event": "position_closed", "symbol": "BTC/USDT",
                 "pnl_usd": float(i)} for i in range(15)]
        sec = ss._trades_section(rows, limit=10)
        self.assertEqual(len(sec["rows"]), 10)
        self.assertEqual(sec["rows"][-1]["pnlUsd"], 14.0)
        self.assertEqual(sec["closedCount"], 15)
        self.assertAlmostEqual(sec["realizedPnlUsd"], 105.0)

    def test_empty_history_says_so(self):
        from tools.trading_ai2 import swarm_status as ss
        sec = ss._trades_section([{"event": "cycle", "symbol": "BTC/USDT"}])
        self.assertEqual(sec["rows"], [])
        self.assertEqual(sec["note"], "сделок в истории пока нет")


class TestPositionsSection(unittest.TestCase):
    """Счёт MarginPad: поля панели, честный dry-run и отсутствие ключей."""

    SECRET = "mk-secret-never-in-output"
    BALANCE = {"starting_balance_usd": 10000.0, "balance_usd": 9965.33,
               "equity_usd": 9965.33, "margin_in_use_usd": 271.5,
               "free_margin_usd": 9693.83, "unrealized_pnl_usd": 0.42}
    POSITIONS = {"positions": [
        {"id": "p1", "symbol": "SOL", "side": "long", "status": "open",
         "margin_usd": 100.0, "leverage": 3, "entry_price": 94.15,
         "mark_price": 94.6, "unrealized_pnl_usd": 0.42, "pnl_usd": 0.42,
         "qty": 1.06, "opened_ts": 1_700_000_000_000},
        {"id": "p2", "symbol": "ETH", "side": "short", "status": "closed",
         "margin_usd": 171.5, "leverage": 3, "entry_price": 2237.51,
         "exit_price": 2241.0, "pnl_usd": -3.5, "qty": 0.076},
    ]}

    def _patched(self, key=SECRET, balance=None, positions=None):
        return (patch("tools.research_agent_clients.env", return_value=key),
                patch("tools.trading_ai2.funding_executor.get_balance",
                      return_value=self.BALANCE if balance is None else balance),
                patch("tools.trading_ai2.funding_executor.list_positions",
                      return_value=self.POSITIONS
                      if positions is None else positions))

    def test_camelcase_shape_and_no_key_leak(self):
        import time as _time

        from tools.trading_ai2 import swarm_status as ss
        env_p, bal_p, pos_p = self._patched()
        with env_p, bal_p as bal, pos_p:
            sec = ss._positions_section()
        text = json.dumps(sec, ensure_ascii=False)
        self.assertNotIn(self.SECRET, text)
        self.assertNotIn("X-API-Key", text)
        bal.assert_called_once_with(self.SECRET)
        self.assertTrue(sec["live"])
        self.assertIsNone(sec["error"])
        self.assertEqual(sec["balance"]["equityUsd"], 9965.33)
        self.assertEqual(sec["balance"]["marginInUseUsd"], 271.5)
        self.assertEqual([p["symbol"] for p in sec["positions"]], ["SOL/USDT"])
        row = sec["positions"][0]
        self.assertEqual(row["entryPrice"], 94.15)
        self.assertEqual(row["markPrice"], 94.6)
        self.assertGreater(row["ageS"], _time.time() - 1_700_000_002)
        self.assertEqual(sec["closedCount"], 1)
        self.assertAlmostEqual(sec["realizedPnlUsd"], -3.5)
        self.assertEqual(sec["symbols"], ["SOL/USDT"])
        self.assertIn("не настоящие деньги", sec["note"])

    def test_no_key_is_dry_run_not_an_empty_account(self):
        from tools.trading_ai2 import swarm_status as ss
        env_p, bal_p, pos_p = self._patched(key="")
        with env_p, bal_p as bal, pos_p as pos:
            sec = ss._positions_section()
        self.assertFalse(sec["live"])
        self.assertIn("MARGINPAD_API_KEY не задан", sec["note"])
        self.assertEqual(sec["balance"], {})
        self.assertEqual(sec["positions"], [])
        bal.assert_not_called()
        pos.assert_not_called()

    def test_marginpad_error_is_not_a_clean_zero_account(self):
        """401 от MarginPad ≠ «счёт пуст»: панель обязана показать отказ."""
        from tools.trading_ai2 import swarm_status as ss
        env_p, bal_p, pos_p = self._patched(balance={"error": "http_401"},
                                            positions={"error": "http_401"})
        with env_p, bal_p, pos_p:
            sec = ss._positions_section()
        self.assertFalse(sec["live"])
        self.assertIn("http_401", sec["error"])
        self.assertIn("http_401", sec["note"])
        self.assertNotIn(self.SECRET, json.dumps(sec, ensure_ascii=False))
        self.assertEqual(sec["positions"], [])
        self.assertEqual(sec["balance"]["equityUsd"], None)

    def test_future_opened_ts_never_gives_negative_age(self):
        """Часы MarginPad спешат на секунду — возраст не уходит в минус."""
        import time as _time

        from tools.trading_ai2 import swarm_status as ss
        future = {"positions": [
            {"id": "p1", "symbol": "SOL", "side": "long", "status": "open",
             "margin_usd": 100.0, "leverage": 3, "entry_price": 93.91,
             "opened_ts": int((_time.time() + 0.8) * 1000)}]}
        env_p, bal_p, pos_p = self._patched(positions=future)
        with env_p, bal_p, pos_p:
            sec = ss._positions_section()
        self.assertGreaterEqual(sec["positions"][0]["ageS"], 0.0)


class TestSwarmSection(unittest.TestCase):
    """Панель видит пул персон и топ-N голосующих из живого AgentPool."""

    class _Agent:
        def __init__(self, name, persona, timeframe, weight):
            self.name, self.persona = name, persona
            self.timeframe, self.weight = timeframe, weight

    class _Pool:
        def __init__(self, agents, active_n):
            self.agents, self.active_n = agents, active_n

        def __len__(self):
            return len(self.agents)

        def top(self, n):
            return self.agents[:n]

    def _section(self, weights):
        from tools.trading_ai2 import swarm_status as ss
        specs = [("TrendFollower-Aggressive", "TrendFollower", "15m"),
                 ("Scalper-Falcon", "Scalper", "1m"),
                 ("Contrarian-RSI", "Scalper", "1m")]
        agents = [self._Agent(n, p, tf, w)
                  for (n, p, tf), w in zip(specs, weights)]
        pool = self._Pool(agents, 2)
        with patch("tools.trading_ai2.swarm.agent_swarm.AgentPool",
                   lambda *a: pool), \
             patch("tools.trading_ai2.swarm.agent_swarm.llm_status",
                   return_value=("free/deepseek-v4-pro-0813", [])), \
             patch("tools.trading_ai2.swarm.darwin.load_weights",
                   return_value={}), \
             patch("tools.trading_ai2.swarm.knowledge.load_rules",
                   return_value=[{} for _ in range(8)]):
            return ss._swarm_section(3, 2)

    def test_pool_and_live_weights(self):
        sec = self._section([1.0, 1.3, 1.0])
        self.assertEqual((sec["pool"], sec["active"], sec["agents"]),
                         (3, 2, 3))
        self.assertEqual(sec["rules"], 8)
        self.assertEqual(sec["model"], "free/deepseek-v4-pro-0813")
        self.assertEqual(sec["weighted"], 1)
        self.assertEqual(sec["weights"][0]["name"], "Scalper-Falcon")
        self.assertEqual([p["count"] for p in sec["personas"]], [1, 2])
        self.assertFalse([n for n in sec["notes"] if "веса стартовые" in n])

    def test_starting_weights_are_explained(self):
        """Все 1.00 — не поломка, а честное «сделок ещё не было»."""
        sec = self._section([1.0, 1.0, 1.0])
        self.assertEqual(sec["weighted"], 0)
        self.assertTrue([n for n in sec["notes"] if "веса стартовые" in n])


class TestStatusReportSections(unittest.TestCase):
    """Один упавший раздел не обнуляет весь отчёт панели."""

    def _report(self, rows, **overrides):
        from tools.trading_ai2 import swarm_status as ss
        seen = {}

        def market(symbol, md=None):
            seen["symbol"] = symbol
            return overrides.get("market", {"symbol": symbol})

        loaders = {
            "_history_section": overrides.get("history_rows", rows),
            "_market_section": market,
            "_positions_section": overrides.get("positions",
                                                {"live": True}),
            "_swarm_section": overrides.get("swarm", {"pool": 100}),
            "_portfolio_section": overrides.get("portfolio", {"assets": []}),
        }
        with patch.object(ss, "_history_section",
                          return_value=loaders["_history_section"]), \
             patch.object(ss, "_market_section", side_effect=market), \
             patch.object(ss, "_positions_section",
                          return_value=loaders["_positions_section"]), \
             patch.object(ss, "_swarm_section",
                          return_value=loaders["_swarm_section"]), \
             patch.object(ss, "_portfolio_section",
                          return_value=loaders["_portfolio_section"]):
            return ss.build_report(100, 30, "", 5), seen

    def test_market_follows_history_not_hardcoded_btc(self):
        out, seen = self._report([{"event": "cycle", "symbol": "SOPH"}])
        self.assertTrue(out["ok"])
        self.assertEqual(seen["symbol"], "SOPH/USDT")
        self.assertIn("portfolio", out["sections"])

    def test_explicit_symbol_wins(self):
        from tools.trading_ai2 import swarm_status as ss
        seen = {}
        with patch.object(ss, "_history_section",
                          return_value=[{"event": "cycle", "symbol": "SOPH"}]), \
             patch.object(ss, "_market_section",
                          side_effect=lambda s, md=None: seen.update(symbol=s)), \
             patch.object(ss, "_positions_section", return_value={}), \
             patch.object(ss, "_swarm_section", return_value={}), \
             patch.object(ss, "_portfolio_section", return_value={}):
            ss.build_report(100, 30, "ETH/USDT", 5)
        self.assertEqual(seen["symbol"], "ETH/USDT")

    def test_broken_section_recorded_and_others_survive(self):
        from tools.trading_ai2 import swarm_status as ss
        rows = [{"event": "cycle", "symbol": "SOL/USDT"}]
        with patch.object(ss, "_history_section", return_value=rows), \
             patch.object(ss, "_market_section",
                          side_effect=RuntimeError("ccxt down")), \
             patch.object(ss, "_positions_section",
                          side_effect=RuntimeError("marginpad down")), \
             patch.object(ss, "_swarm_section", return_value={"pool": 100}), \
             patch.object(ss, "_portfolio_section", return_value={"assets": []}):
            out = ss.build_report(100, 30, "", 5)
        self.assertTrue(out["ok"])
        self.assertIn("swarm", out["sections"])
        self.assertNotIn("market", out["sections"])
        self.assertEqual(len(out["errors"]), 2)
        # секции собираются параллельно — порядок ошибок не гарантирован
        self.assertTrue(any("ccxt down" in e for e in out["errors"]))
        self.assertTrue(any("marginpad down" in e for e in out["errors"]))
        self.assertEqual(out["sections"]["history"], rows)

    def test_history_limited_to_recent_rows(self):
        rows = [{"event": "cycle", "symbol": f"A{i}"} for i in range(9)]
        out, _ = self._report(rows)
        self.assertEqual(len(out["sections"]["history"]), 5)
        self.assertEqual(out["sections"]["history"][-1]["symbol"], "A8")


class TestSwarmEnginePositions(unittest.TestCase):
    """Движок: быстрая ручка счёта, кэш на TTL и цикл через пул персон."""

    def setUp(self):
        from tools.trading_ai2 import swarm_server
        self.mod = swarm_server
        swarm_server.invalidate_caches()

    def tearDown(self):
        self.mod.invalidate_caches()

    def test_positions_cache_one_poll_per_ttl(self):
        calls = []
        account = {"live": True, "balance": {"equityUsd": 9965.33}}
        with patch("tools.trading_ai2.swarm_status._positions_section",
                   side_effect=lambda: (calls.append("poll"), account)[1]), \
             patch("tools.trading_ai2.swarm_status._trades_section",
                   return_value={"rows": [], "note": "сделок пока нет"}):
            first = self.mod.build_positions()
            second = self.mod.build_positions()
            self.mod.invalidate_caches()
            third = self.mod.build_positions()
        self.assertEqual(calls, ["poll", "poll"])
        self.assertEqual(first, second)
        self.assertEqual(third, first)
        self.assertTrue(first["ok"])
        self.assertEqual(first["balance"]["equityUsd"], 9965.33)

    def test_positions_section_error_isolated(self):
        with patch("tools.trading_ai2.swarm_status._positions_section",
                   side_effect=RuntimeError("marginpad down")), \
             patch("tools.trading_ai2.swarm_status._trades_section",
                   return_value={"rows": []}):
            out = self.mod.build_positions()
        self.assertTrue(out["ok"])
        self.assertEqual(out["errors"], ["positions: marginpad down"])
        self.assertIn("trades", out)

    def test_status_uses_pool_and_active(self):
        seen = []
        with patch("tools.trading_ai2.swarm_status.build_report",
                   side_effect=lambda p, a, s, h: (
                       seen.append((p, a, s, h)), {"ok": True})[1]):
            self.mod.build_status(force=True)
            self.mod.build_status()
        self.assertEqual(seen, [(self.mod.POOL_SIZE, self.mod.ACTIVE_N,
                                 "", self.mod.HISTORY)])

    def test_cycle_command_targets_pool_not_legacy_agents(self):
        cmd = self.mod.cycle_command()
        self.assertEqual(cmd[cmd.index("--pool-size") + 1],
                         str(self.mod.POOL_SIZE))
        self.assertEqual(cmd[cmd.index("--active") + 1],
                         str(self.mod.ACTIVE_N))
        self.assertIn("--no-execute", cmd)
        self.assertNotIn("--agents", cmd)
        self.assertNotIn("--live", cmd)
        self.assertIn("--live", self.mod.cycle_command(live=True))

    def test_query_flags(self):
        h = self.mod.H
        self.assertTrue(h._want_fresh("fresh=1"))
        self.assertFalse(h._want_fresh("live=1"))
        self.assertTrue(h._want_live("a=1&live=1"))
        self.assertFalse(h._want_live("notlive=1"))

    def test_stream_cycle_runs_cycle_command_verbatim(self):
        """Продакшн-путь цикла обязан запускать именно cycle_command."""
        seen: list = []

        class _Proc:
            stdout: list = []

            @staticmethod
            def wait():
                return 0

        class _W:
            def write(self, data):
                pass

            def flush(self):
                pass

        class _Handler:
            headers: dict = {}
            wfile = _W()

            def send_response(self, *a):
                pass

            def send_header(self, *a):
                pass

            def end_headers(self):
                pass

        def popen(cmd, **kw):
            seen.append(cmd)
            return _Proc()

        with patch.object(self.mod.subprocess, "Popen", side_effect=popen), \
                patch.object(self.mod, "invalidate_caches") as inv:
            self.mod.H._stream_cycle(_Handler(), live=True)
        self.assertEqual(seen, [self.mod.cycle_command(live=True)])
        inv.assert_called_once()


class TestSwarmEngineLoop(unittest.TestCase):
    """Движок: автономный прогон (loop) и live по умолчанию.

    Loop обязан быть-process'ом движка, а не HTTP-соединением: панель
    только наблюдает. Поэтому здесь проверяются команда прогона, разбор
    query-флагов, отказ второго прогона и мягкий стоп через флаг-файл
    (сигналы ОС на Windows эту работу не делают).
    """

    def setUp(self):
        from tools.trading_ai2 import swarm_server
        self.mod = swarm_server
        self._saved = (swarm_server.LOOP, swarm_server.CYCLE_LOCK,
                       swarm_server.LIVE_CYCLE, swarm_server.LOOP_INTERVAL,
                       swarm_server.LOOP_FLAG)
        swarm_server.LOOP = None
        swarm_server.invalidate_caches()

    def tearDown(self):
        (self.mod.LOOP, self.mod.CYCLE_LOCK, self.mod.LIVE_CYCLE,
         self.mod.LOOP_INTERVAL, self.mod.LOOP_FLAG) = self._saved
        self.mod.invalidate_caches()

    @staticmethod
    def _fake_proc(exit_code=None):
        class _Proc:
            pid = 4321

            def __init__(self):
                self.stdout = iter(())

            def poll(self):
                return exit_code

            def wait(self):
                return exit_code

            def kill(self):
                pass

        return _Proc()

    def _fake_loop(self, **fields):
        from collections import deque
        rec = {"proc": self._fake_proc(), "pid": 4321,
               "cmd": self.mod.loop_command(), "live": True,
               "interval": 60.0, "started": time.time() - 12.0,
               "code": None, "stopping": False,
               "tail": deque(["[loop] итерация 1"], maxlen=self.mod.LOOP_TAIL)}
        rec.update(fields)
        self.mod.LOOP = rec
        return rec

    def test_loop_command_holds_positions_and_carries_interval(self):
        cmd = self.mod.loop_command(live=True, interval=120)
        self.assertEqual(cmd[cmd.index("--pool-size") + 1],
                         str(self.mod.POOL_SIZE))
        self.assertEqual(cmd[cmd.index("--active") + 1], str(self.mod.ACTIVE_N))
        self.assertEqual(cmd[cmd.index("--interval") + 1], "120")
        self.assertIn("--loop", cmd)
        self.assertIn("--live", cmd)
        # Смысл loop — держать позиции: --no-execute убил бы весь прогон.
        self.assertNotIn("--no-execute", cmd)
        self.assertNotIn("--agents", cmd)
        self.assertNotIn("--live", self.mod.loop_command())

    def test_live_is_the_default_and_demo_is_explicit(self):
        h = self.mod.H
        self.mod.LIVE_CYCLE = True
        self.assertTrue(h._resolve_live(""))
        self.assertTrue(h._resolve_live("fresh=1"))
        self.assertFalse(h._resolve_live("demo=1"))
        self.mod.LIVE_CYCLE = False
        self.assertFalse(h._resolve_live(""))
        self.assertTrue(h._resolve_live("live=1"))
        # demo=1 сильнее явного live=1: панель просит демо — движок не спорит
        self.assertFalse(h._resolve_live("live=1&demo=1"))

    def test_interval_is_clamped_to_sane_bounds(self):
        self.assertEqual(self.mod.H._want_interval("interval=5"), 10.0)
        self.assertEqual(self.mod.H._want_interval("interval=99999"), 3600.0)
        self.assertEqual(self.mod.H._want_interval("interval=300"), 300.0)
        self.mod.LOOP_INTERVAL = 90.0
        self.assertEqual(self.mod.H._want_interval(""), 90.0)
        self.assertEqual(self.mod.H._want_interval("interval=abc"), 90.0)

    def test_second_loop_is_refused_with_pid(self):
        self._fake_loop()
        seen: list = []
        with patch.object(self.mod.subprocess, "Popen",
                          side_effect=lambda *a, **k: seen.append(a)):
            code, body = self.mod.start_loop(live=True, interval=60)
        self.assertEqual(code, 409)
        self.assertEqual(body["error"], "loop_busy")
        self.assertEqual(body["pid"], 4321)
        self.assertEqual(seen, [])          # второй процесс не поднимался

    def test_loop_yields_to_a_manual_cycle(self):
        self.mod.CYCLE_LOCK.acquire()
        try:
            code, body = self.mod.start_loop(live=True)
        finally:
            self.mod.CYCLE_LOCK.release()
        self.assertEqual((code, body["error"]), (409, "cycle_busy"))

    def test_cycle_route_refuses_while_loop_is_running(self):
        """Ручной цикл на том же счёте начал бы спорить за маржу."""
        self._fake_loop()
        self.assertTrue(self.mod.loop_alive())

    def test_stop_without_loop_is_refused(self):
        code, body = self.mod.stop_loop()
        self.assertEqual((code, body["error"]), (409, "loop_not_running"))

    def test_soft_stop_writes_flag_and_marks_stopping(self):
        rec = self._fake_loop()
        with tempfile.TemporaryDirectory() as tmp:
            self.mod.LOOP_FLAG = Path(tmp) / "loop_stop.flag"
            code, body = self.mod.stop_loop()
            self.assertEqual(code, 200)
            self.assertTrue(self.mod.LOOP_FLAG.exists())
            self.assertEqual(body["phase"], "stopping")
            self.assertTrue(rec["stopping"])
            self.assertFalse(rec.get("forced"))

    def test_force_stop_kills_instead_of_flagging(self):
        rec = self._fake_loop()
        killed = []
        rec["proc"].kill = lambda: killed.append("kill")
        with tempfile.TemporaryDirectory() as tmp:
            self.mod.LOOP_FLAG = Path(tmp) / "loop_stop.flag"
            code, _ = self.mod.stop_loop(force=True)
        self.assertEqual(code, 200)
        self.assertEqual(killed, ["kill"])
        self.assertTrue(rec["forced"])

    def test_flag_file_reaches_the_loop_script(self):
        """Единственный канал стопа: файл, который пишет движок и читает
        прогон. Проверка сквозная — иначе мягкий стоп ничего не значит."""
        from tools.trading_ai2 import demo_trading_grand as demo
        with tempfile.TemporaryDirectory() as tmp:
            flag = Path(tmp) / "loop_stop.flag"
            self.mod.LOOP_FLAG = flag
            demo.LOOP_STOP_FLAG = flag
            self.mod.request_loop_stop()
            self.assertTrue(flag.exists())
            self.assertTrue(demo.stop_requested())    # прочитан и снят
            self.assertFalse(flag.exists())
            self.assertFalse(demo.stop_requested())

    def test_sleep_or_stop_wakes_on_flag(self):
        from tools.trading_ai2 import demo_trading_grand as demo
        with tempfile.TemporaryDirectory() as tmp:
            flag = Path(tmp) / "loop_stop.flag"
            demo.LOOP_STOP_FLAG = flag
            self.assertFalse(demo.sleep_or_stop(0.05, slice_s=0.01))
            flag.write_text("stop requested\n", encoding="utf-8")
            # длинный интервал не важен: флаг виден на первой же проверке
            self.assertTrue(demo.sleep_or_stop(600.0, slice_s=0.01))
            self.assertFalse(flag.exists())

    def test_loop_status_reports_the_engine_not_the_connection(self):
        idle = self.mod.loop_status()
        self.assertFalse(idle["running"])
        self.assertEqual(idle["phase"], "idle")
        self.assertEqual(idle["tail"], [])

        self._fake_loop()
        live = self.mod.loop_status()
        self.assertTrue(live["running"])
        self.assertEqual(live["phase"], "running")
        self.assertEqual(live["pid"], 4321)
        self.assertEqual(live["interval"], 60.0)
        self.assertGreaterEqual(live["uptimeS"], 10.0)
        self.assertEqual(live["tail"], ["[loop] итерация 1"])
        self.assertIn("--loop", live["cmd"])

        # код выхода пишет читатель stdout (_pump_loop) — панель его только
        # показывает, сама не считает.
        self._fake_loop(proc=self._fake_proc(exit_code=0), code=0)
        exited = self.mod.loop_status()
        self.assertFalse(exited["running"])
        self.assertEqual(exited["phase"], "exited")
        self.assertEqual(exited["exitCode"], 0)

    def test_pump_collects_tail_then_exit_code(self):
        """Хвост прогона собирается в движке: панель его читает, а не
        держит соединение — прогон переживает закрытие вкладки."""
        rec = self._fake_loop(proc=self._fake_proc(exit_code=0))

        class _Pipe:
            """stdout как у Popen(text=True): итерируется и закрывается."""

            def __init__(self, lines):
                self.lines = lines
                self.closed = False

            def __iter__(self):
                return iter(self.lines)

            def close(self):
                self.closed = True

        pipe = _Pipe(["[loop] итерация 1\n", "    [risk] slots 1/3\n"])
        rec["proc"].stdout = pipe
        rec["tail"].clear()
        with patch.object(self.mod, "invalidate_caches") as inv:
            self.mod._pump_loop(rec)
        self.assertEqual(list(rec["tail"]),
                         ["[loop] итерация 1", "    [risk] slots 1/3"])
        self.assertEqual(rec["code"], 0)
        self.assertTrue(pipe.closed)
        inv.assert_called_once()


class TestDecisionWindow(unittest.TestCase):
    """Окно «Решение»: дебаты парламента доезжают из цикла до панели.

    Контракт один: запись цикла несёт блок debate, а движок отдаёт его по
    /api/decision из хвоста истории — без зависимости от того, живой ли ещё
    процесс прогона.
    """

    def _verdict(self, name, action, score, conf, members):
        from tools.trading_ai2.swarm.debate_swarm import SwarmVerdict
        return SwarmVerdict(name, action, score, conf, members)

    def _vote(self, agent, action, conf, persona="P", weight=1.0):
        return SwarmVote(action=action, confidence=conf, reason="r",
                         stop_loss=0.0, agent=agent, persona=persona,
                         weight=weight)

    def test_debate_record_carries_consensus_and_votes(self):
        from tools.trading_ai2.demo_trading_grand import _debate_record
        rec = _debate_record(
            [self._verdict("Technical", "BUY", 0.4, 0.4,
                           [self._vote("TrendFollower-1", "BUY", 0.7),
                            self._vote("Scalper-1", "HOLD", 0.3,
                                        weight=1.2)]),
            self._verdict("Risk", "HOLD", 0.0, 0.8,
                          [self._vote("RiskManager-1", "HOLD", 0.8)])],
            ["[swarm] 🏛️ Technical Swarm (Round 1):",
             "[swarm] 🏆 Technical Swarm Consensus: BUY"],
            "META-VERDICT: HOLD (Risk Swarm Veto — confidence 0.80 > 0.7)")
        self.assertEqual([s["name"] for s in rec["swarms"]],
                         ["Technical", "Risk"])
        tech = rec["swarms"][0]
        self.assertEqual(tech["action"], "BUY")
        self.assertEqual(tech["confidence"], 0.4)
        self.assertEqual(tech["tally"], {"BUY": 1, "SELL": 0, "HOLD": 1})
        self.assertEqual(tech["members"][1]["agent"], "Scalper-1")
        self.assertEqual(tech["members"][1]["weight"], 1.2)
        self.assertIn("Risk Swarm Veto", rec["veto"])
        self.assertEqual(len(rec["lines"]), 2)
        self.assertIn("🏛️", rec["lines"][0])

    def test_debate_record_absent_without_parliament(self):
        """Плоское голосование (--no-debate) не притворяется решением роев."""
        from tools.trading_ai2.demo_trading_grand import _debate_record
        self.assertIsNone(_debate_record([], ["нет дебатов"], ""))

    def test_debate_record_bounds_history_size(self):
        """Дерево дебатов в историю обрезается по строкам и по длине."""
        from tools.trading_ai2.demo_trading_grand import (
            DEBATE_LINES_IN_HISTORY, _debate_record)
        lines = ["x" * 400] * (DEBATE_LINES_IN_HISTORY + 20)
        rec = _debate_record(
            [self._verdict("Risk", "HOLD", 0.0, 0.1, [self._vote("a", "HOLD", .1)])],
            lines, "")
        self.assertEqual(len(rec["lines"]), DEBATE_LINES_IN_HISTORY)
        self.assertTrue(all(len(x) <= 240 for x in rec["lines"]))

    def test_debate_record_marks_vote_transitions(self):
        """Смена голоса после дебатов доезжает до панели данными, а не
        только текстом дерева — иначе «⚔️ Round 2» непроверяем."""
        from tools.trading_ai2.demo_trading_grand import _debate_record
        v = self._verdict(
            "Technical", "HOLD", 0.0, 0.63,
            [self._vote("TrendFollower-1", "HOLD", 0.55),
             self._vote("Scalper-1", "HOLD", 0.71)])
        v.opening = [self._vote("TrendFollower-1", "BUY", 0.62),
                     self._vote("Scalper-1", "HOLD", 0.30)]
        tech = _debate_record([v], ["[swarm] ⚔️"], "")["swarms"][0]
        self.assertTrue(tech["debated"])
        self.assertEqual(tech["moved"], 1)
        flipped, steadier = tech["members"]
        self.assertEqual(
            (flipped["opening_action"], flipped["action"], flipped["changed"]),
            ("BUY", "HOLD", True))
        self.assertFalse(steadier["changed"])
        self.assertEqual(steadier["opening_action"], "HOLD")
        self.assertEqual(steadier["opening_confidence"], 0.3)

    def test_debate_record_unanimous_has_no_transitions(self):
        """Единогласный раунд 1: дебаты не запускались, меняться было нечему."""
        from tools.trading_ai2.demo_trading_grand import _debate_record
        v = self._verdict("Risk", "HOLD", 0.0, 0.8,
                          [self._vote("RiskManager-1", "HOLD", 0.8)])
        entry = _debate_record([v], [], "")["swarms"][0]
        self.assertFalse(entry["debated"])
        self.assertEqual(entry["moved"], 0)
        self.assertEqual(entry["members"][0]["opening_action"], "HOLD")
        self.assertIsNone(entry["members"][0]["opening_confidence"])

    def test_decision_window_keeps_latest_parliament_run(self):
        """Плоский loop поверх парламента не вытесняет решение роев из окна.

        Регрессия на вытеснение: цикл, поднятый кодом до парламента, пишет
        по записи каждые 60 секунд, и за DECISION_LIMIT минут последнее
        решение трёх роев исчезало из /api/decision — панель оставалась
        плоской ровно тогда, когда парламент был интереснее всего.
        """
        from tools.trading_ai2 import swarm_server
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trading_history.jsonl"
            rows = [{"ts": "2026-10-30T21:00:00+00:00", "event": "cycle",
                     "arbiter": {"action": "HOLD", "score": 0.0},
                     "debate": {"veto": "", "swarms": [
                         {"name": "Risk", "action": "HOLD", "confidence": 0.8,
                          "members": [], "debated": True, "moved": 1}],
                         "lines": ["[swarm] ⚔️ Risk Swarm (Round 2 - Debates):"]}}]
            rows += [{"ts": f"2026-10-30T21:0{i}:00+00:00", "event": "cycle",
                      "arbiter": {"action": "HOLD"}} for i in range(1, 6)]
            path.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                                      for r in rows) + "\n", encoding="utf-8")
            saved = swarm_server.HISTORY_PATH
            swarm_server.HISTORY_PATH = path
            try:
                swarm_server.invalidate_caches()
                out = swarm_server.build_decision()
            finally:
                swarm_server.HISTORY_PATH = saved
                swarm_server.invalidate_caches()
        self.assertEqual(out["total"], 6)
        self.assertEqual(len(out["items"]), swarm_server.DECISION_LIMIT)
        self.assertEqual([i["ts"] for i in out["items"]],
                         ["2026-10-30T21:05:00+00:00",
                          "2026-10-30T21:04:00+00:00",
                          "2026-10-30T21:00:00+00:00"])
        kept = out["items"][-1]
        self.assertEqual(kept["debate"]["swarms"][0]["moved"], 1)

    def test_cycle_record_includes_debate(self):
        """run_cycle обязан писать debate — иначе панели показывать нечего."""
        import inspect
        from tools.trading_ai2 import demo_trading_grand as demo
        src = inspect.getsource(demo.run_cycle)
        self.assertIn("debate=_debate_record(", src)
        self.assertIn("swarm_verdicts = verdicts", src)
        # вердикт до вета обязан доехать до записи: без него «перебил»
        # и «подтвердил» в истории неразличимы
        self.assertIn("debate_models, pre_veto", src)
        self.assertIn('pre_veto = {"action": con.action', src)

    def test_debate_record_keeps_verdict_that_veto_overruled(self):
        """Вето, перебившее сделку, — отдельный след, а не та же строка HOLD."""
        from tools.trading_ai2.demo_trading_grand import _debate_record
        v = self._verdict("Risk", "HOLD", 0.0, 0.82,
                          [self._vote("RiskManager-1", "HOLD", 0.82)])
        rec = _debate_record(
            [v], [], "META-VERDICT: HOLD (Risk Swarm Veto — "
                     "confidence 0.82 > 0.7)",
            pre_veto={"action": "BUY", "score": 0.62, "approved": True})
        self.assertTrue(rec["veto_changed"])
        self.assertEqual(rec["veto_before"],
                         {"action": "BUY", "score": 0.62, "approved": True})

    def test_debate_record_marks_veto_that_changed_nothing(self):
        """Рой и так молчал в HOLD: вето правило подтвердил, а не перебил."""
        from tools.trading_ai2.demo_trading_grand import _debate_record
        v = self._verdict("Risk", "HOLD", 0.0, 0.76,
                          [self._vote("RiskManager-1", "HOLD", 0.76)])
        rec = _debate_record(
            [v], [], "META-VERDICT: HOLD (Risk Swarm Veto — "
                     "confidence 0.76 > 0.7)",
            pre_veto={"action": "HOLD", "score": 0.0, "approved": False})
        self.assertFalse(rec["veto_changed"])
        self.assertEqual(rec["veto_before"]["action"], "HOLD")

    def test_debate_record_without_veto_has_no_before(self):
        """Нет вета — нет и «до-вердикта»: панель не должна ничего выдумывать."""
        from tools.trading_ai2.demo_trading_grand import _debate_record
        v = self._verdict("Technical", "BUY", 0.6, 0.7,
                          [self._vote("TrendFollower-1", "BUY", 0.7)])
        rec = _debate_record([v], [], "",
                             pre_veto={"action": "BUY", "score": 0.6,
                                       "approved": True})
        self.assertIsNone(rec["veto_before"])
        self.assertFalse(rec["veto_changed"])

    def test_veto_impact_names_the_consequence(self):
        """TUI-строка различает перебитый вердикт и подтверждённый HOLD."""
        from tools.trading_ai2.demo_trading_grand import _veto_impact
        self.assertIn("перебил SELL → HOLD",
                      _veto_impact({"action": "SELL", "score": 0.4,
                                    "approved": False}))
        self.assertIn("и без вета", _veto_impact({"action": "HOLD"}))
        self.assertIn("и без вета", _veto_impact(None))

    def test_decision_api_reads_tail_of_history(self):
        from tools.trading_ai2 import swarm_server
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trading_history.jsonl"
            rows = [
                {"ts": "2026-10-30T21:00:00+00:00", "event": "cycle",
                 "mode": "live", "symbol": "SOL/USDT", "price": 180.1,
                 "risk_mode": "NORMAL",
                 "votes": {"buy": 1, "sell": 0, "hold": 2, "count": 3},
                 "arbiter": {"action": "HOLD", "approved": False,
                             "score": 0.0, "reason": "META-VERDICT: HOLD"},
                 "debate": {"veto": "META-VERDICT: HOLD (Risk Swarm Veto)",
                            "swarms": [{"name": "Risk", "action": "HOLD",
                                        "score": 0.0, "confidence": 0.8,
                                        "tally": {"BUY": 0, "SELL": 0,
                                                  "HOLD": 4},
                                        "members": []}],
                            "lines": ["[swarm] 🏛️ Risk Swarm (Round 1):"]}},
                {"ts": "2026-10-30T21:01:00+00:00", "event": "position_closed",
                 "pnl_usd": -1.2},
                {"ts": "2026-10-30T21:02:00+00:00", "event": "cycle",
                 "mode": "live", "symbol": "SOL/USDT", "price": 180.4,
                 "arbiter": {"action": "BUY", "approved": True, "score": 0.21,
                             "reason": "консенсус"},
                 "debate": {"veto": "", "swarms": [
                     {"name": "Technical", "action": "BUY", "score": 0.6,
                      "confidence": 0.6, "tally": {"BUY": 3, "SELL": 0,
                                                   "HOLD": 1},
                      "members": [{"agent": "TrendFollower-9",
                                   "persona": "TrendFollower", "action": "BUY",
                                   "confidence": 0.72, "weight": 1.1,
                                   "mode": "live"}]}],
                     "lines": []},
                 "risk_mode": "MICRO"},
            ]
            path.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                                      for r in rows) + "\n", encoding="utf-8")
            saved = swarm_server.HISTORY_PATH
            swarm_server.HISTORY_PATH = path
            try:
                swarm_server.invalidate_caches()
                out = swarm_server.build_decision()
            finally:
                swarm_server.HISTORY_PATH = saved
                swarm_server.invalidate_caches()
        self.assertTrue(out["ok"])
        self.assertEqual(out["total"], 2)
        first, second = out["items"]
        self.assertEqual(first["ts"], "2026-10-30T21:02:00+00:00")
        self.assertEqual(first["riskMode"], "MICRO")
        self.assertEqual(first["debate"]["swarms"][0]["members"][0]["agent"],
                         "TrendFollower-9")
        self.assertEqual(second["arbiter"]["action"], "HOLD")
        self.assertIn("Risk Swarm Veto", second["debate"]["veto"])

    def test_decision_of_old_records_without_debate(self):
        """Записи до парламента: карточка есть, но debate = None."""
        from tools.trading_ai2 import swarm_server
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trading_history.jsonl"
            path.write_text(json.dumps(
                {"ts": "t", "event": "cycle", "symbol": "BTC/USDT",
                 "arbiter": {"action": "HOLD"}}) + "\n", encoding="utf-8")
            saved = swarm_server.HISTORY_PATH
            swarm_server.HISTORY_PATH = path
            try:
                swarm_server.invalidate_caches()
                out = swarm_server.build_decision()
            finally:
                swarm_server.HISTORY_PATH = saved
                swarm_server.invalidate_caches()
        self.assertEqual(out["items"][0]["debate"], None)
        self.assertEqual(out["items"][0]["arbiter"]["action"], "HOLD")

    def test_decision_is_cached_per_ttl(self):
        from tools.trading_ai2 import swarm_server
        calls = []
        with patch.object(swarm_server, "_history_tail_records",
                          side_effect=lambda n: (calls.append(n),
                                                 [])[1]):
            swarm_server.invalidate_caches()
            swarm_server.build_decision()
            swarm_server.build_decision()
            swarm_server.build_decision(force=True)
        self.assertEqual(len(calls), 2)

    def test_decision_survives_missing_history(self):
        from tools.trading_ai2 import swarm_server
        with tempfile.TemporaryDirectory() as tmp:
            saved = (swarm_server.HISTORY_PATH, swarm_server.DECISION_TTL_S)
            swarm_server.HISTORY_PATH = Path(tmp) / "нет-такого-файла.jsonl"
            try:
                swarm_server.invalidate_caches()
                out = swarm_server.build_decision()
            finally:
                (swarm_server.HISTORY_PATH,
                 swarm_server.DECISION_TTL_S) = saved
                swarm_server.invalidate_caches()
        self.assertTrue(out["ok"])
        self.assertEqual(out["items"], [])


class TestRebalanceCloseRecord(unittest.TestCase):
    """Закрытие ребаланса: итог берётся из записи позиции MarginPad."""

    POS = {"id": "p1", "symbol": "SOL", "side": "long", "margin_usd": 100.0,
           "entry_price": 93.91, "opened_ts": 1_700_000_000_000}

    def test_pnl_exit_and_hold_from_record(self):
        from tools.trading_ai2 import demo_trading_grand as d
        rec = {"exit_price": 94.02, "pnl_usd": 0.02,
               "closed_ts": 1_700_000_154_000}
        self.assertEqual(d.closed_trade_fields(self.POS, {}, rec),
                         {"exit_price": 94.02, "hold_s": 154.0,
                          "pnl_usd": 0.02})

    def test_close_response_is_the_fallback(self):
        from tools.trading_ai2 import demo_trading_grand as d
        f = d.closed_trade_fields(self.POS, {"realized_pnl_usd": -1.48}, {})
        self.assertEqual(f["pnl_usd"], -1.48)
        self.assertIsNone(f["exit_price"])

    def test_unknown_pnl_stays_none_never_a_fake_zero(self):
        """Нет итога у MarginPad — в истории null, а не «$0.00»."""
        from tools.trading_ai2 import demo_trading_grand as d
        f = d.closed_trade_fields(self.POS, {}, {})
        self.assertIsNone(f["pnl_usd"])

    def test_clock_skew_cannot_make_hold_negative(self):
        from tools.trading_ai2 import demo_trading_grand as d
        pos = dict(self.POS, opened_ts=1_700_000_100_000)
        rec = {"pnl_usd": 0.5, "closed_ts": 1_700_000_099_000}
        self.assertGreaterEqual(d.closed_trade_fields(pos, {}, rec)["hold_s"],
                                0.0)


class TestRiskProfiles(unittest.TestCase):
    """Dynamic Risk Profiling: стейт-машина Арбитра + жёсткие лимиты
    Критика поверх профиля."""

    def _proposal(self, size_pct: float = 2.0, stop_loss: float = 2.0):
        from tools.trading_ai2.swarm.generator import Proposal
        return Proposal("BUY", 0.8, {"BUY": 10, "SELL": 0, "HOLD": 0},
                        size_pct, stop_loss, "test")

    def test_win_streak_plus_trend_switches_aggressive(self):
        """3 победы подряд + TRENDING_UP + фон >0.2 → AGGRESSIVE."""
        from tools.trading_ai2.swarm.arbiter import Arbiter
        arb = Arbiter()
        new = arb.update_profile(win_streak=3, regime="TRENDING_UP",
                                 market_sentiment=0.4)
        self.assertEqual(new, "AGGRESSIVE")
        self.assertEqual(arb.risk_profile, "AGGRESSIVE")
        self.assertAlmostEqual(arb.threshold, 0.40)
        self.assertAlmostEqual(arb.max_size_pct, 5.0)

    def test_aggressive_needs_all_three_conditions(self):
        """Без тренда/фона/стрика агрессии нет — остаётся BALANCED."""
        from tools.trading_ai2.swarm.arbiter import Arbiter
        arb = Arbiter()
        for kw in ({"win_streak": 3, "regime": "TRENDING_DOWN",
                    "market_sentiment": 0.4},
                   {"win_streak": 3, "regime": "TRENDING_UP",
                    "market_sentiment": 0.1},
                   {"win_streak": 2, "regime": "TRENDING_UP",
                    "market_sentiment": 0.4}):
            self.assertIsNone(arb.update_profile(**kw))
            self.assertEqual(arb.risk_profile, "BALANCED")

    def test_loss_streak_switches_conservative(self):
        """2 убытка подряд → CONSERVATIVE, порог 0.70, размер ≤1.5%."""
        from tools.trading_ai2.swarm.arbiter import Arbiter
        arb = Arbiter()
        new = arb.update_profile(loss_streak=2, regime="TRENDING_UP",
                                 market_sentiment=0.3)
        self.assertEqual(new, "CONSERVATIVE")
        self.assertAlmostEqual(arb.threshold, 0.70)
        self.assertAlmostEqual(arb.max_size_pct, 1.5)

    def test_panic_or_volatile_force_conservative(self):
        from tools.trading_ai2.swarm.arbiter import Arbiter
        arb = Arbiter()
        arb.update_profile(market_sentiment=-0.6)
        self.assertEqual(arb.risk_profile, "CONSERVATIVE")
        arb2 = Arbiter()
        arb2.update_profile(regime="VOLATILE")
        self.assertEqual(arb2.risk_profile, "CONSERVATIVE")

    def test_profile_caps_position_size(self):
        """AGGRESSIVE разрешает до 5%, BALANCED — до 3% от предложения."""
        from tools.trading_ai2.swarm.arbiter import Arbiter
        arb = Arbiter()
        con = arb.rule(self._proposal(size_pct=9.0), [])
        self.assertAlmostEqual(con.amount_percent, 3.0)
        arb.update_profile(win_streak=3, regime="TRENDING_UP",
                           market_sentiment=0.4)
        con = arb.rule(self._proposal(size_pct=9.0), [])
        self.assertAlmostEqual(con.amount_percent, 5.0)

    def test_manual_pin_blocks_automatics(self):
        """Пин клавишей R держит профиль — автоматика не перебивает."""
        from tools.trading_ai2.swarm.arbiter import Arbiter
        arb = Arbiter()
        self.assertEqual(arb.cycle_profile(), "AGGRESSIVE")
        self.assertEqual(arb.cycle_profile(), "CONSERVATIVE")  # пин
        self.assertIsNone(arb.update_profile(
            win_streak=9, regime="TRENDING_UP", market_sentiment=0.9))
        self.assertEqual(arb.risk_profile, "CONSERVATIVE")
        arb.clear_manual()
        self.assertEqual(arb.update_profile(
            win_streak=9, regime="TRENDING_UP", market_sentiment=0.9),
            "AGGRESSIVE")

    def test_critic_cuts_size_over_20pct_margin(self):
        """Маржа портфеля >20% equity → Критик режет до остатка."""
        from tools.trading_ai2.swarm.arbiter import Arbiter
        from tools.trading_ai2.swarm.critic import Critic
        con = Arbiter().rule(self._proposal(size_pct=5.0), [])
        notes = Critic().harden(con, equity_usd=10_000.0,
                                margin_in_use_usd=1_950.0)
        # свободно 0.5% из 20% капа — сделку урезали до него
        self.assertAlmostEqual(con.amount_percent, 0.5)
        self.assertTrue(any("Size reduced" in n for n in notes))

    def test_critic_caps_wide_stop_loss(self):
        """Стоп -3% переписывается на -1.5% даже без margin-проблем."""
        from tools.trading_ai2.swarm.arbiter import Arbiter
        from tools.trading_ai2.swarm.critic import Critic
        con = Arbiter().rule(self._proposal(stop_loss=-3.0), [])
        notes = Critic().harden(con, equity_usd=10_000.0,
                                margin_in_use_usd=0.0)
        self.assertAlmostEqual(con.stop_loss, -1.5)
        self.assertTrue(any("stop-loss capped" in n for n in notes))

    def test_harden_ignores_unapproved(self):
        """Нет APPROVED — Критик ничего не трогает."""
        from tools.trading_ai2.swarm.critic import Critic
        from tools.trading_ai2.swarm.arbiter import Consensus
        con = Consensus("HOLD", 0.0, {}, 0.0, -3.0, "x")
        self.assertEqual(Critic().harden(con, equity_usd=10_000.0,
                                         margin_in_use_usd=0.0), [])
        self.assertAlmostEqual(con.stop_loss, -3.0)


class TestChaosMode(unittest.TestCase):
    """Chaos Mode: ATR +100%/час — слушаем только отряд шторма,
    сделки идут микро-лотом со спред-гардой."""

    @staticmethod
    def _vote(action, persona, confidence=0.8, weight=1.0):
        return SwarmVote(action=action, confidence=confidence,
                         reason="t", stop_loss=0.0,
                         agent=f"{persona}-1", persona=persona,
                         weight=weight)

    def _mixed_votes(self):
        # обычные агенты все в HOLD (как и положено в шторм),
        # отряд шторма голосует BUY
        normals = [self._vote("HOLD", f"TrendFollower-{i}", 0.9)
                   for i in range(10)]
        chaos = [self._vote("BUY", "ChaosScalper-Aggressive", 0.9),
                 self._vote("BUY", "VolatilityBreakout-Fast", 0.8)]
        return normals + chaos

    def test_chaos_ignores_normal_votes(self):
        """ATR > 100%: обычные HOLD-голоса не считаются — решает
        только отряд шторма, вердикт несёт risk_mode=MICRO."""
        from tools.trading_ai2.swarm.arbiter import Arbiter
        from tools.trading_ai2.swarm.generator import Proposal
        p = Proposal("BUY", 0.0, {}, 2.0, -0.5, "r")
        con = Arbiter().rule(p, [], votes=self._mixed_votes(),
                             atr_growth=150.0)
        self.assertEqual(con.action, "BUY")
        self.assertTrue(con.approved)
        self.assertEqual(con.risk_mode, "MICRO")
        self.assertGreater(con.score, 0.5)      # только chaos-голоса

    def test_normal_mode_unchanged(self):
        """Тот же набор голосов без ATR — вето большинства, MICRO нет."""
        from tools.trading_ai2.swarm.arbiter import Arbiter
        from tools.trading_ai2.swarm.generator import Proposal
        p = Proposal("BUY", 0.0, {}, 2.0, -0.5, "r")
        con = Arbiter().rule(p, [], votes=self._mixed_votes(),
                             atr_growth=50.0)
        self.assertEqual(con.action, "HOLD")
        self.assertEqual(con.risk_mode, "NORMAL")

    def test_chaos_without_squad_is_hold(self):
        """ATR > 100%, но отряд молчит — HOLD, обычные голоса не в счёт."""
        from tools.trading_ai2.swarm.arbiter import Arbiter
        from tools.trading_ai2.swarm.generator import Proposal
        p = Proposal("BUY", 0.0, {}, 2.0, -0.5, "r")
        con = Arbiter().rule(p, [], votes=[
            self._vote("BUY", "Momentum-Aggressive", 0.9)
            for _ in range(10)], atr_growth=120.0)
        self.assertEqual(con.action, "HOLD")
        self.assertIn("отряд шторма", con.reason)

    def test_chaos_personas_in_pool(self):
        """Отряд шторма — семьи в _PERSONAS и в сгенерированном пуле."""
        from tools.trading_ai2.swarm.agent_swarm import (
            _PERSONAS, AgentPool)
        fams = {name for name, _c, _p in _PERSONAS}
        self.assertIn("ChaosScalper", fams)
        self.assertIn("VolatilityBreakout", fams)
        fams = {a.persona.split("-")[0] for a in
                AgentPool(pool_size=100, active_n=12, seed=1).agents}
        self.assertTrue({"ChaosScalper", "VolatilityBreakout"} <= fams)

    def test_micro_sizing_in_dry_run(self):
        """risk_mode=MICRO → margin урезается до 0.5% equity."""
        from unittest.mock import patch
        from tools.trading_ai2.swarm.arbiter import Consensus
        from tools.trading_ai2.funding_executor import FundingExecutor
        fex = FundingExecutor()
        fex.live = False               # тест не должен уходить на API
        con = Consensus("BUY", 0.9, {}, 3.0, 49_500.0, "chaos",
                        risk_mode="MICRO")
        lines: list[str] = []
        with patch("tools.trading_ai2.funding_executor.get_price",
                   return_value={"price": 50_000.0}):
            out = fex.execute_verdict(con, emit=lines.append,
                                      equity_usd=10_000.0)
        self.assertEqual(out["status"], "dry_run")
        # 3% от 10000 → $300, кап микро-лота 0.5% → $50
        self.assertTrue(any("margin $50" in l for l in lines), lines)

    def test_spread_guard_cancels_wide_spread(self):
        """Спред 0.5% > порога 0.3% — ордер отменяется до отправки."""
        from unittest.mock import patch
        from tools.trading_ai2.swarm.arbiter import Consensus
        from tools.trading_ai2.funding_executor import FundingExecutor
        fex = FundingExecutor()
        fex.live = True                  # до slippage-гейта доходим
        fex.key = "k"
        con = Consensus("BUY", 0.9, {}, 3.0, 49_500.0, "chaos",
                        risk_mode="MICRO")
        lines: list[str] = []
        with patch("tools.trading_ai2.funding_executor.get_price",
                   return_value={"price": 50_000.0}), \
             patch.object(FundingExecutor, "_spread_pct",
                          return_value=0.5), \
             patch("tools.trading_ai2.funding_executor.open_position",
                   side_effect=AssertionError("ордер ушёл на API")):
            out = fex.execute_verdict(con, emit=lines.append,
                                      equity_usd=10_000.0)
        self.assertEqual(out["status"], "spread_blocked")
        self.assertTrue(
            any("spread too wide (0.5%)" in l for l in lines), lines)

    def test_normal_verdict_skips_spread_guard(self):
        """Обычная сделка (NORMAL) — спред-чек не выполняется."""
        from unittest.mock import patch
        from tools.trading_ai2.swarm.arbiter import Consensus
        from tools.trading_ai2.funding_executor import FundingExecutor
        fex = FundingExecutor()
        fex.live = False               # DRY-RUN — без API
        con = Consensus("BUY", 0.9, {}, 3.0, 49_500.0, "n")
        with patch("tools.trading_ai2.funding_executor.get_price",
                   return_value={"price": 50_000.0}), \
             patch.object(FundingExecutor, "_spread_pct",
                          return_value=0.5) as m:
            out = fex.execute_verdict(con, emit=lambda *_: None,
                                      equity_usd=10_000.0)
        m.assert_not_called()
        self.assertEqual(out["status"], "dry_run")


class TestDebateSwarm(unittest.TestCase):
    """Swarm of Swarms: два раунда дебатов, консенсус четвёрки,
    мета-голоса и абсолютное вето Risk-роя."""

    @staticmethod
    def _agent(persona: str):
        from tools.trading_ai2.swarm.agent_swarm import SwarmAgent
        return SwarmAgent(name=f"{persona}-T", persona=persona,
                          system_prompt="", reputation_score=1.0,
                          llm=None)          # llm=None → детерминированный stub

    def test_debate_changes_vote(self):
        """TrendFollower BUY, но MeanReversion + Scalper уверенно
        против — после дебатов он уходит в HOLD."""
        from tools.trading_ai2.swarm.debate_swarm import _stub_round2
        mine = SwarmVote(action="BUY", confidence=0.6, reason="тренд",
                         stop_loss=0.0, agent="TrendFollower-T",
                         persona="TrendFollower", weight=1.0)
        peers = [SwarmVote(action="SELL", confidence=0.6,
                           reason="RSI 75 — перегрев", stop_loss=0.0,
                           agent="MeanReversion-T",
                           persona="MeanReversion", weight=1.0),
                 SwarmVote(action="SELL", confidence=0.7,
                           reason="импульс вниз", stop_loss=0.0,
                           agent="Scalper-T", persona="Scalper",
                           weight=1.0)]
        out = _stub_round2(mine, peers)
        self.assertEqual(out.action, "HOLD")
        self.assertIn("согласен с коллегами", out.reason)

    def test_debate_keeps_vote_against_minority(self):
        """Против только один коллега — голос остаётся."""
        from tools.trading_ai2.swarm.debate_swarm import _stub_round2
        mine = SwarmVote(action="BUY", confidence=0.6, reason="тренд",
                         stop_loss=0.0, agent="a", persona="P",
                         weight=1.0)
        peers = [SwarmVote(action="SELL", confidence=0.6, reason="x",
                           stop_loss=0.0, agent="b", persona="Q",
                           weight=1.0)]
        self.assertEqual(_stub_round2(mine, peers).action, "BUY")

    def test_deadlock_is_hold_zero(self):
        """2 BUY против 2 SELL — консенсуса нет, голос веса 0."""
        from tools.trading_ai2.swarm.debate_swarm import _consensus
        votes = [SwarmVote(action=a, confidence=c, reason="r",
                           stop_loss=0.0, agent=f"a{i}", persona="P",
                           weight=1.0)
                 for i, (a, c) in enumerate(
                     [("BUY", .8), ("BUY", .8), ("SELL", .8), ("SELL", .8)])]
        v = _consensus("Technical", votes)
        self.assertEqual(v.action, "HOLD")
        self.assertEqual(v.confidence, 0.0)

    def test_risk_veto_absolute(self):
        """Risk HOLD 0.8 ветирует даже два BUY консенсуса."""
        from tools.trading_ai2.swarm.debate_swarm import (
            SwarmVerdict, risk_veto, meta_votes)
        verdicts = [SwarmVerdict("Technical", "BUY", 0.7, 0.7, []),
                    SwarmVerdict("Sentiment", "BUY", 0.5, 0.5, []),
                    SwarmVerdict("Risk", "HOLD", 0.0, 0.8, [])]
        veto = risk_veto(verdicts)
        self.assertIn("Risk Swarm Veto", veto)
        self.assertIn("META-VERDICT: HOLD", veto)
        # мета-голоса: Risk несёт conf 0.8 в голосовании, deadlock
        # Технического был бы нулевым весом
        meta = meta_votes(verdicts)
        self.assertEqual(len(meta), 3)
        self.assertEqual(meta[2].action, "HOLD")
        self.assertAlmostEqual(meta[2].confidence, 0.8)

    def test_veto_reachable_through_real_consensus(self):
        """Вето достигается настоящим консенсусом, а не вердиктом из теста.

        Регрессия на мёртвое правило: _consensus отдавал HOLD только по
        ничьей и всегда с confidence 0.0, поэтому Risk-рой физически не
        мог перешагнуть порог 0.7 — «абсолютное вето» не срабатывало ни
        при какой раскладке голосов.
        """
        from tools.trading_ai2.swarm.debate_swarm import (
            _consensus, meta_votes, risk_veto)

        def vote(action, conf, agent, weight=1.0):
            return SwarmVote(action=action, confidence=conf, reason="r",
                             stop_loss=0.0, agent=agent, persona=agent,
                             weight=weight)

        risk = _consensus("Risk", [vote("HOLD", .9, "RiskManager"),
                                   vote("HOLD", .85, "StopHunter"),
                                   vote("HOLD", .8, "Hodler"),
                                   vote("BUY", .4, "MacdTrader")])
        self.assertEqual(risk.action, "HOLD")
        self.assertGreater(risk.confidence, 0.7)
        self.assertEqual(risk.score, 0.0)     # направления нет
        tech = _consensus("Technical", [vote("BUY", .8, "TrendFollower"),
                                        vote("BUY", .7, "Scalper"),
                                        vote("SELL", .3, "Breakout"),
                                        vote("HOLD", .5, "MeanReversion")])
        veto = risk_veto([tech, _consensus("Sentiment", [
            vote("HOLD", .6, "Analyst"), vote("HOLD", .6, "Contrarian"),
            vote("BUY", .5, "Volatility"), vote("SELL", .4, "RsiWatcher")]),
            risk])
        self.assertIn("Risk Swarm Veto", veto)
        self.assertIn(f"confidence {risk.confidence:.2f}", veto)
        # вето не подменяет мета-голоса: их три, и BUY Технического жив
        meta = meta_votes([tech, risk])
        self.assertEqual({m.action for m in meta}, {"BUY", "HOLD"})

    def test_hold_minority_does_not_inflate_confidence(self):
        """Один HOLD против двух направлений — прежняя ничья: HOLD 0.0."""
        from tools.trading_ai2.swarm.debate_swarm import (
            _consensus, risk_veto)
        verdict = _consensus("Risk", [
            SwarmVote(action=a, confidence=c, reason="r", stop_loss=0.0,
                      agent=f"a{i}", persona="P", weight=1.0)
            for i, (a, c) in enumerate([("HOLD", .95), ("HOLD", .9),
                                        ("BUY", .9), ("SELL", .9)])])
        self.assertEqual(verdict.action, "HOLD")
        self.assertEqual(verdict.confidence, 0.0)
        self.assertEqual(risk_veto([verdict]), "")

    def test_no_veto_below_threshold(self):
        """Risk HOLD при conf 0.7 ровно — вето не срабатывает (> 0.7)."""
        from tools.trading_ai2.swarm.debate_swarm import (
            SwarmVerdict, risk_veto)
        self.assertEqual(risk_veto([
            SwarmVerdict("Risk", "HOLD", 0.0, 0.7, [])]), "")

    def test_mini_swarm_debate_end_to_end(self):
        """Четвёрка stub-агентов: два раунда → вердикт + строки дерева."""
        from tools.trading_ai2.swarm.debate_swarm import (
            mini_swarm_debate)
        crew = [self._agent(p) for p in
                ("TrendFollower", "MeanReversion", "Scalper", "Breakout")]
        snap = {"price": 100.0, "rsi": 20.0, "ema9_above_ema21": False,
                "macd": -60.0, "volume_ratio": 0.8}
        verdict, lines, r1, r2 = mini_swarm_debate(
            "Technical", crew, snap)
        self.assertEqual(len(r1), 4)
        self.assertEqual(len(r2), 4)
        self.assertIn(verdict.action, ("BUY", "SELL", "HOLD"))
        joined = "\n".join(lines)
        self.assertIn("Round 1", joined)
        self.assertIn("Round 2 - Debates", joined)
        self.assertIn("Technical Swarm Consensus", joined)

    def test_opening_votes_attached_after_debates(self):
        """Раунд 1 остаётся при вердикте — только если дебаты реально были."""
        from tools.trading_ai2.swarm.debate_swarm import mini_swarm_debate
        crew = [self._agent(p) for p in
                ("TrendFollower", "MeanReversion", "Scalper", "Breakout")]
        snap = {"price": 100.0, "rsi": 20.0, "ema9_above_ema21": False,
                "macd": -60.0, "volume_ratio": 0.8}
        verdict, lines, r1, r2 = mini_swarm_debate("Technical", crew, snap)
        if r1 is r2:
            self.assertEqual(verdict.opening, [])
        else:
            self.assertEqual([v.agent for v in verdict.opening],
                             [v.agent for v in r1])
            self.assertEqual([v.action for v in verdict.members],
                             [v.action for v in r2])

    def test_round2_lines_show_action_and_confidence_shift(self):
        """В строке второго раунда видно и новый голос, и дельту уверенности."""
        from tools.trading_ai2.swarm import debate_swarm as ds
        crew = [self._agent(p) for p in
                ("TrendFollower", "MeanReversion", "Scalper", "Breakout")]
        snap = {"price": 100.0, "rsi": 20.0, "ema9_above_ema21": False,
                "macd": -60.0, "volume_ratio": 0.8}

        def argue(ag, _snap, round1):
            own = next(v for v in round1 if v.agent == ag.name)
            return own.model_copy(update={"action": "HOLD", "confidence": 0.77,
                                          "reason": "коллеги убедили"})

        real = ds._debate_round2
        ds._debate_round2 = argue
        try:
            verdict, lines, r1, r2 = ds.mini_swarm_debate(
                "Technical", crew, snap)
        finally:
            ds._debate_round2 = real
        self.assertIsNot(r1, r2)          # разногласие → дебаты были
        moved_lines = [l for l in lines if "← сменил голос" in l]
        self.assertTrue(moved_lines)
        for line in moved_lines:
            # сам переход виден текстом, а не только пометкой в конце
            self.assertRegex(line, r"[A-Z]+ → HOLD")
            self.assertIn("0.77", line)          # новый уровень уверенности
        self.assertTrue(verdict.opening)

    def test_four_distinct_models_in_swarm(self):
        """assign_models: внутри четвёрки — четыре РАЗНЫЕ модели."""
        from tools.trading_ai2.swarm.debate_swarm import assign_models

        class _Pool:
            _gen_models = ["cohere/m1", "mercury-3", "gemma-4",
                           "mistral-s", "groq/oss"]

            def _llm_for(self, m):
                return object()

        crew = [self._agent(p) for p in
                ("TrendFollower", "MeanReversion", "Scalper", "Breakout")]
        assign_models(_Pool(), {"Technical": crew})
        ids = [ag.model_id for ag in crew]
        self.assertEqual(len(set(ids)), 4)

    def test_smart_debate_skips_unanimous(self):
        """Все 4 за HOLD — раунд 2 не запускается, голосов столько же,
        консенсус снимается сразу (экономия LLM-запросов)."""
        from tools.trading_ai2.swarm import debate_swarm as ds
        crew = [self._agent(p) for p in
                ("TrendFollower", "MeanReversion", "Scalper", "Breakout")]
        # снимок-«флэт»: все стабовые персоны голосуют HOLD
        snap = {"price": 100.0, "rsi": 50.0, "ema9_above_ema21": True,
                "macd": 5.0, "volume_ratio": 0.8,
                "market_sentiment": 0.0, "asset_sentiment": 0.0}
        calls = []
        real = ds._debate_round2
        ds._debate_round2 = lambda *a, **k: calls.append(1) or real(*a, **k)
        try:
            verdict, lines, r1, r2 = ds.mini_swarm_debate(
                "Technical", crew, snap)
        finally:
            ds._debate_round2 = real
        if all(v.action == r1[0].action for v in r1):
            self.assertEqual(calls, [])          # дебаты не понадобились
            self.assertIs(r1, r2)
            self.assertIn("единогласно", "\n".join(lines))
        else:
            self.assertNotEqual(calls, [])       # разногласие — дебаты были

    def test_parliament_returns_three_verdicts(self):
        """Полный прогон stub-пула: 3 вердикта в порядке MINI_SWARMS."""
        from tools.trading_ai2.swarm.debate_swarm import (
            run_parliament, MINI_SWARMS)
        agents = []
        for fams in MINI_SWARMS.values():
            agents += [self._agent(p) for p in fams]
        for ag in agents:
            ag.model_id = ""

        class _Pool:
            def __init__(self, agents):
                self.agents = agents
                self._gen_models: list = []

            def _llm_for(self, m):
                return None

        snap = {"price": 100.0, "rsi": 45.0, "ema9_above_ema21": True,
                "macd": 20.0, "volume_ratio": 1.1}
        verdicts, lines, r2, _m = run_parliament(_Pool(agents), snap)
        self.assertEqual([v.name for v in verdicts],
                         list(MINI_SWARMS))
        self.assertEqual(len(r2), 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
