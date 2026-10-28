"""Tests: verified answer -> payment; unverified/empty -> null; LoopGuard denies."""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loop_guard import LoopGuard
from near_intent_payer import NearIntentPayer
import research_agent_demo as ra


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

    result = asyncio.run(orchestrator_demo.main("verified swarm task"))
    assert result["architect"] == "architect.near"
    assert result["stages"]["research"]["winner"] == "research-agent"
    assert result["stages"]["audit"]["output"]["verdict"] == "pass"
    expected_commands = ["decompose"] + [
        command
        for _ in range(3)
        for command in ("hire", "verify", "create_intent")
    ]
    assert [entry["command"] for entry in result["audit_log"]] == expected_commands
    assert len(result["payment_receipts"]) == 3
    assert all(receipt["status"] == "settled" for receipt in result["payment_receipts"])
    assert len(result["rejected_bids"]) == 7


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
