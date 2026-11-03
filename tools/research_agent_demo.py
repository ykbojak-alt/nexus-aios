"""Research Agent MVP — ODSC x Nebius hackathon scaffold.

Pipeline: query -> Tavily search -> context filter -> Nebius synthesis -> cite check.
API keys via env only: TAVILY_API_KEY, NEBIUS_API_KEY, NEBIUS_MODEL (default provided).
Runs on mock data when keys are absent so the structure stays testable.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import urllib.request
from dataclasses import dataclass

TAVILY_URL = "https://api.tavily.com/search"
# Synthesis backend: OpenAI-compatible. Defaults to Inception Mercury (no billing),
# Nebius Token Factory can be used by overriding SYNTH_BASE_URL/SYNTH_API_KEY.
SYNTH_BASE_URL = os.environ.get("SYNTH_BASE_URL", "https://api.inceptionlabs.ai/v1")
SYNTH_KEY_ENV = os.environ.get("SYNTH_KEY_ENV", "INCEPTION_API_KEY")
NEBIUS_URL = f"{SYNTH_BASE_URL}/chat/completions"
NEBIUS_MODEL = os.environ.get("NEBIUS_MODEL", "mercury-3")
MAX_SOURCES = 8
CONTENT_LIMIT = 1200

SOURCE_RE = re.compile(r"\[Source:\s*(https?://[^\]\s]+)\]")
CONTEXT_URL_RE = re.compile(r"^URL:\s*(https?://\S+)$", re.MULTILINE)

SYSTEM_PROMPT = """You are a careful research assistant. Answer using only the supplied SOURCES.
Support factual claims with citations in this exact format: [Source: exact-url].
Use URLs exactly as supplied. If the sources do not support an answer, say so.
Do not invent facts or citations."""


@dataclass
class Source:
    index: int
    title: str
    url: str
    content: str


def _post_json(url: str, payload: dict, headers: dict | None = None) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


async def search_tavily(query: str) -> list[Source]:
    """Tavily search -> deduped, truncated sources. Mock when TAVILY_API_KEY is absent."""
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        return [
            Source(1, "Mock result A", "https://example.com/a", "Mock content for: " + query),
            Source(2, "Mock result B", "https://example.com/b", "Second mock context block."),
        ]
    data = _post_json(TAVILY_URL, {
        "api_key": key, "query": query, "max_results": MAX_SOURCES,
        "search_depth": "advanced", "include_answer": False,
    })
    seen, sources = set(), []
    for r in data.get("results", []):
        url = r.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)
        sources.append(Source(len(sources) + 1, r.get("title", ""), url,
                              (r.get("content") or "")[:CONTENT_LIMIT]))
    return sources[:MAX_SOURCES]


def build_context(sources: list[Source]) -> str:
    return "\n\n".join(
        f"[{s.index}] {s.title}\nURL: {s.url}\n{s.content}" for s in sources
    )


async def synthesize_nebius(query: str, context: str) -> str:
    """Synthesis via OpenAI-compatible API strictly over context."""
    key = os.environ.get(SYNTH_KEY_ENV) or os.environ.get("NEBIUS_API_KEY")
    if not key:
        source_url = next(iter(CONTEXT_URL_RE.findall(context)), None)
        if source_url is None:
            return "The supplied sources do not contain a citable URL."
        return f"Mock synthesis for '{query}' based on context. [Source: {source_url}]"
    data = _post_json(NEBIUS_URL, {
        "model": NEBIUS_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"QUESTION: {query}\n\nSOURCES:\n{context}"},
        ],
        "temperature": 0.1,
    }, headers={"Authorization": f"Bearer {key}"})
    return data["choices"][0]["message"]["content"]


def cite_check(answer: str, sources: list[Source]) -> tuple[str, list[str]]:
    """Verify every [Source: url] exists among fetched sources. Flag unverified."""
    valid = {s.url for s in sources}
    cited = SOURCE_RE.findall(answer)
    unverified = [u for u in cited if u not in valid]
    for u in unverified:
        answer = answer.replace(f"[Source: {u}]", f"[⚠ unverified: {u}]")
    return answer, unverified


async def run_research(query: str, payer=None, task_id: str | None = None) -> dict:
    sources = await search_tavily(query)
    if not sources:
        return {"query": query, "answer": "В доступных источниках информации не найдено",
                "sources": [], "unverified": [], "verified": False, "payment": None}
    context = build_context(sources)
    raw = await synthesize_nebius(query, context)
    answer, unverified = cite_check(raw, sources)
    cited = SOURCE_RE.findall(raw)
    verified = bool(cited) and not unverified
    payment = None
    if payer is not None and verified:
        if payer.loop_guard is not None:
            payer.loop_guard.require("verify", payer.payer_role)
        payment = payer.pay(
            worker_id="research-agent",
            task_id=task_id or "task-" + hashlib.sha256(query.encode("utf-8")).hexdigest()[:8],
            amount=1.0,
        )
    return {"query": query, "answer": answer,
            "sources": [{"url": s.url, "title": s.title} for s in sources],
            "unverified": unverified, "verified": verified, "payment": payment}


def _load_loop_agent():
    """Живой loop-агент (tools/research_agent.py) — импорт в обоих контекстах."""
    try:
        from research_agent import from_env  # плоский контекст: tools/ в sys.path
    except ImportError:
        from tools.research_agent import from_env  # пакетный контекст
    return from_env()


def live_configured() -> bool:
    """Есть ли ключи для живого цикла (LLM + Tavily)."""
    try:
        from research_agent_clients import env
    except ImportError:
        try:
            from tools.research_agent_clients import env
        except ImportError:
            return False
    return bool(env("LLM_API_KEY") and env("TAVILY_API_KEY"))


def run_research_loop(query: str, payer=None, task_id: str | None = None,
                      agent=None) -> dict:
    """Живой Plan→Act→Observe≤3 → cite_check → тот же payment-контракт.

    agent=None → реальный агент из .env; без ключей — честный отказ.
    В dict добавляются iterations/trace — судьям виден реальный цикл.
    """
    if agent is None:
        if not live_configured():
            return {"query": query, "mode": "live_unavailable",
                    "answer": ("Нет ключей LLM_API_KEY/TAVILY_API_KEY — живой "
                               "агент не запускается; моков не подставляем."),
                    "sources": [], "unverified": [], "verified": False,
                    "payment": None, "iterations": 0, "trace": []}
        agent = _load_loop_agent()
    res = agent.run(query)
    sources = [Source(i + 1, s.get("title", ""), s.get("url", ""),
                      s.get("content", ""))
               for i, s in enumerate(res.sources)]
    answer, unverified = cite_check(res.answer, sources)
    cited = SOURCE_RE.findall(res.answer)
    urls_ok = getattr(res, "verified", True)
    verified = res.ok and bool(cited) and not unverified and urls_ok
    payment = None
    if payer is not None and verified:
        if payer.loop_guard is not None:
            payer.loop_guard.require("verify", payer.payer_role)
        payment = payer.pay(
            worker_id="research-agent",
            task_id=task_id or "task-" + hashlib.sha256(query.encode("utf-8")).hexdigest()[:8],
            amount=1.0,
        )
    return {"query": query, "mode": "live", "answer": answer,
            "sources": [{"url": s.url, "title": s.title} for s in sources],
            "unverified": unverified, "verified": verified, "payment": payment,
            "url_status": getattr(res, "url_status", {}),
            "iterations": res.iterations, "trace": res.trace}


if __name__ == "__main__":
    import asyncio
    import sys
    try:
        from near_intent_payer import NearIntentPayer
    except ImportError:
        from tools.near_intent_payer import NearIntentPayer
    q = " ".join(sys.argv[1:]) or "What is Nebius Token Factory?"
    payer = NearIntentPayer()  # verified answer → расчёт; без конфига — stub/simulated
    if live_configured():      # ключи есть → живой цикл Plan→Act→Observe
        res = run_research_loop(q, payer=payer)
    else:                      # нет ключей → мок-контур, как раньше
        res = asyncio.run(run_research(q, payer=payer))
    print(json.dumps(res, ensure_ascii=False, indent=2))

    try:
        from ansi_demo import link as _ln, red as _rd, gold as _gd
    except ImportError:
        try:
            from tools.ansi_demo import link as _ln, red as _rd, gold as _gd
        except ImportError:
            _ln = _rd = _gd = str

    pay = res.get("payment")
    if pay and pay.get("tx_hash"):   # реальный testnet-перевод — хэш для видео/судей
        print("🪙 " + _ln(f"TxHash: {pay['tx_hash']}"))
        print("   " + _ln(f"Explorer: {pay.get('explorer', '')}"))
    elif pay:                        # чек есть, но отправка не случилась — причина видна
        reason = pay.get("error") or pay.get("note") or pay.get("status")
        print(_rd(f"Payment failed: {pay.get('status')} — {reason}"))
    elif not res.get("verified"):    # платёжа не было потому что ответ не верифицирован
        print(_gd("Payment skipped: answer not verified "
                  "(нет валидных [Source:] цитат из вайтлист-источников)"))
    else:                            # verified, но payer не был передан (API-вызовы без демо)
        print(_gd("Payment skipped: payer not configured"))
