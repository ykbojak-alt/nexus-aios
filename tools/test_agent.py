"""Tests: verified answer -> payment; unverified/empty -> null; LoopGuard denies."""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loop_guard import LoopGuard
from near_intent_payer import NearIntentPayer
import research_agent_demo as ra

# Юнит-тесты строго офлайн: NEAR_* и .env не должны включать testnet/LLM
# контур — иначе появление .env с ключами превратит тесты в живые отправки.
for _n in ("NEAR_ACCOUNT_ID", "NEAR_PRIVATE_KEY", "NEAR_RECEIVER_ID",
           "NEAR_DEMO_AMOUNT", "NEAR_RPC_URL", "NEAR_ESCROW_CONTRACT",
           "NEAR_AGENT_ACCOUNT", "NEAR_AGENT_PRIVATE_KEY",
           "NEAR_AGENT_KEYS_JSON", "NEAR_CREDENTIALS_DIR"):
    os.environ.pop(_n, None)
try:
    import research_agent_clients as _rac
    _rac._DOTENV_CACHE = {}
    _rac._AI2_CACHE = {}
except ImportError:
    pass


def _install_verified_mock():
    original_search = ra.search_tavily
    original_synthesis = ra.synthesize_nebius

    async def fake_search(query):
        return [ra.Source(1, "Verified source", "https://example.org/verified", "Evidence.")]

    async def fake_synthesis(query, context):
        return f"Verified answer. [Source: https://example.org/verified]"

    ra.search_tavily = fake_search
    ra.synthesize_nebius = fake_synthesis
    return original_search, original_synthesis


def _restore_research(originals):
    ra.search_tavily, ra.synthesize_nebius = originals


def test_verified_pays():
    payer = NearIntentPayer()
    originals = _install_verified_mock()
    try:
        result = asyncio.run(ra.run_research("mock question", payer=payer))
    finally:
        _restore_research(originals)
    assert result["sources"], "mock sources expected"
    assert result["unverified"] == []
    assert result["verified"] is True
    assert result["payment"] is not None
    assert result["payment"]["intent_id"].startswith("intent-")
    assert result["payment"]["solver"] == "research-agent"
    assert len(payer.receipts) == 1


def test_uncited_answer_does_not_pay():
    payer = NearIntentPayer()
    originals = _install_verified_mock()

    async def uncited_synthesis(query, context):
        return "An uncited claim."

    ra.synthesize_nebius = uncited_synthesis
    try:
        result = asyncio.run(ra.run_research("uncited question", payer=payer))
    finally:
        _restore_research(originals)
    assert result["verified"] is False
    assert result["payment"] is None
    assert payer.receipts == []


def test_unverified_no_pay():
    payer = NearIntentPayer()
    bad = "Fact. [Source: https://fake.example/x]"
    orig = ra.synthesize_nebius

    async def fake_synth(q, ctx):
        return bad

    ra.synthesize_nebius = fake_synth
    try:
        result = asyncio.run(ra.run_research("q", payer=payer))
    finally:
        ra.synthesize_nebius = orig
    assert result["unverified"]
    assert result["payment"] is None
    assert payer.receipts == []


def test_empty_no_pay():
    payer = NearIntentPayer()
    orig = ra.search_tavily

    async def fake_search(q):
        return []

    ra.search_tavily = fake_search
    try:
        result = asyncio.run(ra.run_research("q", payer=payer))
    finally:
        ra.search_tavily = orig
    assert "не найдено" in result["answer"]
    assert result["payment"] is None


def test_loopguard_denies_sign():
    guard = LoopGuard()
    entry = guard.check("sign_tx", "coder")
    assert entry["verdict"] == "denied"
    try:
        guard.require("sign_tx", "coder")
        raise AssertionError("should have raised")
    except PermissionError:
        pass


def test_loopguard_allows_and_audits():
    guard = LoopGuard()
    e = guard.require("create_intent", "architect")
    assert e["verdict"] == "allowed"
    payer = NearIntentPayer(loop_guard=guard)
    r = payer.pay("w1", "t1", 1.0)
    assert r["status"] == "settled"
    assert len(guard.audit_log) >= 2


def test_orchestrator_audits_hire_verify_and_payment():
    import orchestrator_demo

    # research-стадия — стаб: контракт аукциона проверяется офлайн.
    # Живой мост покрыт test_run_research_loop_* в tests/test_research_agent.py.
    orig_live = ra.live_configured
    ra.live_configured = lambda: False
    try:
        result = asyncio.run(orchestrator_demo.main("verified swarm task"))
    finally:
        ra.live_configured = orig_live
    assert result["architect"] == "architect.near"
    assert result["winner"]["worker"] == "research-agent"
    assert result["winner"]["price"] <= result["task"]["budget"]
    assert result["output"]["skill"] == "research"
    # Контрактные вызовы тоже проходят LoopGuard — аудит честно шире,
    # чем у off-chain версии (submit_bid/freelancer — только on-chain,
    # поэтому здесь их нет).
    assert [e["command"] for e in result["audit_log"]] == [
        "decompose", "hire", "create_task", "accept_bid", "submit_work",
        "verify", "create_intent"]
    assert len(result["payment_receipts"]) == 1
    receipt = result["payment_receipts"][0]
    assert receipt["status"] == "settled"
    assert receipt["solver"] == "research-bot.testnet"
    assert receipt["intent"]["action"] == "pay_for_service"
    # freelancer с битой ed25519-подписью отвергнут — дешевизна не спасла
    assert [b["worker"] for b in result["rejected_bids"]] == ["freelancer"]
    assert result["rejected_bids"][0]["reason"] == "ed25519_verify failed"
    assert any("REJECTED" in l for l in result["auction_log"])
    assert any("победитель" in l for l in result["auction_log"])


def test_intent_payload_structure():
    """Intent — декларация оплаты сервиса, а не голый transfer."""
    payer = NearIntentPayer()
    r = payer.pay("research-agent", "t1", 0.03, service="Tavily_Search")
    it = r["intent"]
    assert it["action"] == "pay_for_service"
    assert it["service"] == "Tavily_Search"
    assert it["amount"] == 0.03 and it["currency"] == "NEAR"
    assert it["intent_id"] == r["intent_id"]
    assert it["solver"] == "research-agent"
    assert it["task_id"] == "t1"
    assert it["deadline_ms"] > it["ts"]
    # default-сервис выводится из worker_id
    r2 = NearIntentPayer().pay("coder", "t2", 1.0)
    assert r2["intent"]["service"] == "coder:agent_work"


def test_auction_rejects_over_budget():
    """Бюджет 0.01 < всех котировок → верифицированных bids нет."""
    import orchestrator_demo
    orig_live = ra.live_configured
    ra.live_configured = lambda: False
    try:
        result = asyncio.run(
            orchestrator_demo.run_auction("q", budget=0.01))
    finally:
        ra.live_configured = orig_live
    assert result["winner"] is None
    assert result["payment_receipts"] == []
    reasons = {b["worker"]: b["reason"] for b in result["rejected_bids"]}
    assert reasons["research-agent"] == "over budget"
    assert reasons["analyst"] == "over budget"
    assert reasons["freelancer"] == "over budget"  # цена отсекается раньше подписи


def test_grand_demo_offline_flow():
    """Grand demo end-to-end офлайн: аукцион → intent → stub-чек → ответ."""
    import hackathon_grand_demo as gd
    orig_live = ra.live_configured
    ra.live_configured = lambda: False
    try:
        res = asyncio.run(gd.main("test query", emit=None))
    finally:
        ra.live_configured = orig_live
    a = res["auction"]
    assert a["winner"]["worker"] == "research-agent"
    assert a["winner"]["price"] <= a["task"]["budget"]
    pay = res["payment"]
    assert pay["status"] == "settled" and pay["mode"] == "stub"
    assert pay["intent"]["action"] == "pay_for_service"
    assert pay["intent"]["service"] == "Tavily_Search"
    assert res["answer"]
    assert [e["command"] for e in a["audit_log"]] == [
        "decompose", "hire", "create_task", "accept_bid", "submit_work",
        "verify", "create_intent"]


def test_trading_demo_offline_flow():
    """Аукцион стратегий end-to-end офлайн: momentum выигрывает торг,
    quant_fund отсекается по чексумме, paper-цикл и stub-чек комиссии."""
    import trading_agent_demo as td
    res = td.run_demo(feed=td.MockFeed(), wait_s=0, emit=None)
    assert res["winner"]["worker"] == "momentum"
    assert [b["worker"] for b in res["rejected_bids"]] == ["quant_fund"]
    assert res["rejected_bids"][0]["reason"] == "checksum mismatch"
    assert res["payment"]["status"] == "settled"
    assert res["payment"]["intent"]["action"] == "pay_for_service"
    assert res["report"]["mode"] == "stub_prices"
    assert res["report"]["trades"] >= 1
    cmds = [e["command"] for e in res["audit_log"]]
    assert cmds[0] == "decompose" and "risk_check" in cmds
    assert "create_intent" in cmds
    denied = [e for e in res["audit_log"] if e["command"] == "transfer_near"]
    assert denied and denied[0]["verdict"] == "denied"


def test_trading_stop_loss_and_drawdown_enforced():
    """Падение ниже −1.5% → stop-loss закрывает позицию; крах −60% на всех
    позициях → drawdown > 5% → halt через LoopGuard."""
    import trading_agent_demo as td
    ticks = (
        {"BTCUSDT": (100.0, 2.40), "ETHUSDT": (50.0, -1.80),
         "NEARUSDT": (10.0, 5.10), "SOLUSDT": (20.0, -3.20),
         "DOGEUSDT": (1.0, 0.60)},
        {"BTCUSDT": (101.0, 2.80), "ETHUSDT": (50.0, -2.60),
         "NEARUSDT": (9.80, 3.0), "SOLUSDT": (19.8, -4.0),
         "DOGEUSDT": (1.0, 1.0)},
    )
    out = []
    res = td.run_demo(feed=td.MockFeed(ticks), wait_s=0, emit=out.append)
    closed = [p for p in res["cycle"]["positions"] if p["closed"]]
    assert closed and closed[0]["symbol"] == "NEARUSDT"
    assert any("stop-loss" in l for l in out)

    crash = (
        {s: (100.0, 3.0) for s in td.SYMBOLS},
        {s: (40.0, -50.0) for s in td.SYMBOLS},
    )
    out2 = []
    res2 = td.run_demo(feed=td.MockFeed(crash), wait_s=0, emit=out2.append)
    assert res2["cycle"]["halted"] is True
    assert any("drawdown limit" in l for l in out2)
    assert any(e["command"] == "halt" for e in res2["audit_log"])


def test_nebius_api_uses_embedded_prompt_not_cwd_file():
    original_key = os.environ.get("NEBIUS_API_KEY")
    original_post = ra._post_json
    payloads = []

    def fake_post(url, payload, headers=None):
        payloads.append(payload)
        return {"choices": [{"message": {"content": "ready"}}]}

    os.environ["NEBIUS_API_KEY"] = "test-key"
    ra._post_json = fake_post
    try:
        answer = asyncio.run(ra.synthesize_nebius("question", "source context"))
    finally:
        ra._post_json = original_post
        if original_key is None:
            os.environ.pop("NEBIUS_API_KEY", None)
        else:
            os.environ["NEBIUS_API_KEY"] = original_key
    assert answer == "ready"
    assert "[Source: exact-url]" in payloads[0]["messages"][0]["content"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
    print("all tests passed")
