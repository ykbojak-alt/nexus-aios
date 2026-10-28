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
NEBIUS_URL = "https://api.tokenfactory.nebius.com/v1/chat/completions"
NEBIUS_MODEL = os.environ.get("NEBIUS_MODEL", "meta-llama/Llama-3.3-70B-Instruct")
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
    """Nebius Token Factory (OpenAI-compatible) synthesis strictly over context."""
    key = os.environ.get("NEBIUS_API_KEY")
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


if __name__ == "__main__":
    import asyncio
    import sys
    q = " ".join(sys.argv[1:]) or "What is Nebius Token Factory?"
    print(json.dumps(asyncio.run(run_research(q)), ensure_ascii=False, indent=2))
