# Reference implementation from the main NEXUS AiOS runtime.
#
# Verbatim copy of ai2/runtime/research_agent.py for hackathon judges —
# shows the SAME agent loop running inside the runtime: model calls go
# through roles.call_model (budget, metrics, router choice, trajectory),
# Tavily is emitted as tool_call events, and each loop step is a
# research_iteration trajectory event. NOT runnable standalone: it
# imports ..models / .roles from the private runtime. The runnable
# standalone twin lives in ../../tools/research_agent.py.

"""Research Agent для рантайма AI-2 — живой Plan → Act → Observe цикл.

Интегрирован, а не приклеен сбоку: вызовы модели идут через
roles.call_model → бюджет, метрики, траектория прогона и router_choice
работают как у исполнителя. Поиск — Tavily REST по urllib (stdlib, без
новых зависимостей): ключ AI2_TAVILY_API_KEY (или TAVILY_API_KEY).

Включение: env AI2_RESEARCH_AGENT=1 или префикс задачи «research:» /
«исследуй:». Без ключа Tavily — честный отказ до первого вызова модели,
исполнитель не маскирует это ответом.

События траектории: research_iteration (plan/search/observe) — судьям
виден реальный цикл агента, а не один вызов LLM.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

from ..models.base import text_message
from .roles import RunContext, call_model, system_prompt

MAX_ITERATIONS = int(os.environ.get("AI2_RESEARCH_MAX_ITER", "3"))
RESULT_SNIPPET = 900
TAVILY_ENDPOINT = "https://api.tavily.com/search"

TASK_PREFIXES = ("research:", "исследуй:", "исследование:")


def enabled() -> bool:
    return os.environ.get("AI2_RESEARCH_AGENT", "").strip().lower() in {
        "1", "true", "yes", "on"}


def is_research_task(task: str) -> bool:
    t = task.strip().lower()
    return enabled() or any(t.startswith(p) for p in TASK_PREFIXES)


def strip_prefix(task: str) -> str:
    """«research: <вопрос>» → «<вопрос>»; без префикса — как есть."""
    t = task.strip()
    for p in TASK_PREFIXES:
        if t.lower().startswith(p):
            return t[len(p):].strip() or t
    return t


def tavily_key() -> str:
    return (os.environ.get("AI2_TAVILY_API_KEY")
            or os.environ.get("TAVILY_API_KEY") or "").strip()


PLAN_SYSTEM = (
    "Ты — исследовательский агент. По вопросу пользователя сформулируй ОДИН "
    "короткий поисковый запрос (3-10 слов), который найдёт нужные факты. "
    "ЖЁСТКОЕ ПРАВИЛО: если в истории уже были поиски и они не дали ответа "
    "(sufficient: false), ты ОБЯЗАН сгенерировать НОВЫЙ запрос — перефразируй, "
    "используй другие ключевые слова, синонимы или уточняющие термины. "
    "Запрещено повторять предыдущие запросы из списка УЖЕ ПРОБОВАЛИ. "
    "Всегда пиши на том же языке, на котором задан вопрос пользователя. "
    "Верни только JSON: {\"query\": \"...\"}."
)

OBSERVE_SYSTEM = (
    "Ты оцениваешь, хватает ли собранных источников, чтобы честно ответить "
    "на вопрос. Верни только JSON: "
    '{"sufficient": true} если данных достаточно, иначе '
    '{"sufficient": false, "next_query": "уточняющий запрос"}. '
    "Если sufficient=false, ты ОБЯЗАН вернуть в поле next_query НЕПУСТУЮ "
    "строку с новым поисковым запросом — никогда не возвращай null или "
    "пустую строку. Запрещено просто добавлять к старому запросу суффикс "
    "вроде (overview) — сформулируй принципиально новый запрос. "
    "next_query обязан отличаться от уже опробованных запросов и пишется "
    "на языке вопроса."
)

ANSWER_SYSTEM = (
    "Ты — исследовательский агент. Ответь на вопрос ТОЛЬКО по приведённым "
    "источникам. Каждое утверждение, опирающееся на источник, заканчивай "
    "ссылкой [Source: <url>]. Если данных не хватает — прямо скажи, чего не "
    "хватает, не выдумывай. Ответ — связный текст. "
    "Всегда отвечай на том же языке, на котором задан вопрос пользователя "
    "(например, если вопрос на русском — ответ на русском)."
)


def _json_obj(text: str) -> dict[str, Any]:
    m = re.search(r"\{[\s\S]*\}", text or "")
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


def _sources_block(sources: list[dict[str, Any]]) -> str:
    lines = []
    for i, s in enumerate(sources, 1):
        lines.append(
            f"[{i}] {s.get('title', '')}\nURL: {s.get('url', '')}\n"
            f"{(s.get('content') or '')[:RESULT_SNIPPET]}")
    return "\n\n".join(lines) or "(источников нет)"


def _norm(query: str) -> str:
    return re.sub(r"\s+", " ", query.strip().lower())


_DEDUPE_SUFFIXES = (" (details)", " (explained)", " (обзор)", " (latest)")

# Дефолтный хинт, когда observe не дала next_query: НОВЫЙ ракурс от вопроса,
# а не суффикс к прошлому запросу (иначе рекурсивное «(overview) (overview)»).
_DEFAULT_ANGLES = ("overview", "guide", "requirements", "faq", "explained")


def _default_hint(question: str, tried_count: int) -> str:
    angle = _DEFAULT_ANGLES[max(0, tried_count - 1) % len(_DEFAULT_ANGLES)]
    return f"{question.rstrip('?!. ')} {angle}"


def _dedupe_query(query: str, past: list[str]) -> str:
    """Повтор запроса принудительно варьируется — иначе поисковик выдаст
    те же результаты и цикл застрянет."""
    tried = {_norm(q) for q in past}
    if _norm(query) not in tried:
        return query
    for suffix in _DEDUPE_SUFFIXES:
        candidate = f"{query}{suffix}"
        if _norm(candidate) not in tried:
            return candidate
    return f"{query} (попытка {len(past) + 1})"


def tavily_search(query: str, api_key: str, *, max_results: int = 5,
                  timeout_s: float = 30.0, retries: int = 3) -> list[dict[str, Any]]:
    """Реальный HTTP к Tavily с ретраями; последняя ошибка поднимается."""
    payload = json.dumps({
        "api_key": api_key, "query": query, "max_results": max_results,
        "search_depth": "basic", "include_answer": False,
    }).encode("utf-8")
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                TAVILY_ENDPOINT, data=payload,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                data = json.loads(r.read().decode("utf-8"))
            return [{
                "title": x.get("title", ""), "url": x.get("url", ""),
                "content": x.get("content", ""), "score": x.get("score", 0.0),
            } for x in data.get("results", [])]
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
    raise RuntimeError(f"tavily: {retries} попытки, последняя ошибка — {last}")


class ResearchAgent:
    """Agent Loop поверх RunContext: та же роль executor, живой инструмент-поиск."""

    def __init__(self, ctx: RunContext, api_key: str | None = None):
        self.ctx = ctx
        self.api_key = api_key if api_key is not None else tavily_key()

    def _chat(self, system: str, user: str, *, max_tokens_hint: int = 0) -> str:
        del max_tokens_hint  # лимит токенов — у рантайма (AI2_MAX_TOKENS_PER_CALL)
        resp = call_model(self.ctx, "executor", system_prompt("executor", system),
                          [text_message("user", user)], stream=False)
        return resp.text or ""

    def _search(self, query: str) -> list[dict[str, Any]]:
        t0 = time.monotonic()
        try:
            out = tavily_search(query, self.api_key)
            err = None
        except Exception as e:  # noqa: BLE001
            out, err = [], str(e)
        self.ctx.traj.emit("tool_call", tool="tavily_search", side_effect="read_only",
                           input={"query": query},
                           result={"results": [{"url": s["url"], "title": s["title"]}
                                               for s in out]} if not err else err,
                           is_error=err is not None, role="executor",
                           retrieval_scores=[s["score"] for s in out] or None,
                           duration_ms=round((time.monotonic() - t0) * 1000, 1))
        if err:
            raise RuntimeError(err)
        return out

    def run(self, question: str) -> str:
        if not self.api_key:
            raise RuntimeError(
                "Research Agent: не задан AI2_TAVILY_API_KEY — живой поиск "
                "недоступен; без ключа агент честно не запускается")

        question = strip_prefix(question)
        sources: list[dict[str, Any]] = []
        seen: set[str] = set()
        queries_tried: list[str] = []
        next_hint: str | None = None

        for it in range(1, MAX_ITERATIONS + 1):
            # Plan — LLM формулирует запрос, видя историю и подсказку прошлой оценки
            user = f"ВОПРОС: {question}"
            if queries_tried:
                user += ("\n\nУЖЕ ПРОБОВАЛИ (запрещено повторять):\n"
                         + "\n".join(f"- {q}" for q in queries_tried))
            if next_hint:
                user += f"\n\nПРЕДЫДУЩАЯ ОЦЕНКА ПРЕДЛОЖИЛА ЗАПРОС: {next_hint}"
            if sources:
                user += f"\n\nУЖЕ СОБРАНО:\n{_sources_block(sources)}"
            query_text = self._chat(PLAN_SYSTEM, user)
            query = _json_obj(query_text).get("query")
            if isinstance(query, str) and query.strip():
                query = query.strip()
            else:
                query = next_hint or question.strip()[:120]
            # Модель залипла на уже опробованном запросе → один ретрай с
            # явным замечанием; финальный дедуп суффиксом — ниже, перед поиском.
            if _norm(query) in {_norm(q) for q in queries_tried}:
                retry_text = self._chat(
                    PLAN_SYSTEM,
                    user + f"\n\nЗапрос «{query}» уже опробован и не дал "
                           "ответа. Сгенерируй ПРИНЦИПИАЛЬНО другой запрос — "
                           "другие слова, другой ракурс.")
                q2 = _json_obj(retry_text).get("query")
                if isinstance(q2, str) and q2.strip():
                    query = q2.strip()
            # Страховка перед поиском: повтор любого прошлого запроса
            # принудительно варьируется суффиксом — иначе та же выдача.
            query = _dedupe_query(query, queries_tried)
            queries_tried.append(query)
            self.ctx.traj.emit("research_iteration", iteration=it, step="plan", query=query)

            # Act — реальный поиск
            try:
                found = self._search(query)
            except RuntimeError as e:
                self.ctx.traj.emit("research_iteration", iteration=it, step="search",
                                   query=query, error=str(e))
                found = []
            fresh = [s for s in found if s.get("url") and s["url"] not in seen]
            for s in fresh:
                seen.add(s["url"])
            sources.extend(fresh)
            self.ctx.traj.emit("research_iteration", iteration=it, step="search",
                               query=query, results=len(found), new=len(fresh))

            # Observe — на последней итерации принудительный синтез, оценка бессмысленна
            if it >= MAX_ITERATIONS:
                break
            observe_user = (f"ВОПРОС: {question}\n\nИСТОЧНИКИ:\n{_sources_block(sources)}"
                            + (f"\n\nУЖЕ ПРОБОВАЛИ ЗАПРОСЫ:\n"
                               + "\n".join(f"- {q}" for q in queries_tried)
                               if queries_tried else ""))
            verdict = _json_obj(self._chat(OBSERVE_SYSTEM, observe_user))
            sufficient = bool(verdict.get("sufficient"))
            nq = verdict.get("next_query")
            if isinstance(nq, str) and nq.strip():
                next_hint = nq.strip()
            elif not sufficient:
                # модель не дала next_query → новый ракурс ОТ ВОПРОСА
                # (не от прошлого запроса — иначе стекинг суффиксов)
                next_hint = _default_hint(question, len(queries_tried))
            else:
                next_hint = None
            self.ctx.traj.emit("research_iteration", iteration=it, step="observe",
                               sufficient=sufficient, next_query=next_hint)
            if sufficient:
                break

        if not sources:
            self.ctx.traj.emit("research_iteration", iteration=it, step="answer",
                               refusal="no_sources")
            return (f"Не удалось найти информацию за {MAX_ITERATIONS} попытки: "
                    "поиск не вернул данных (см. события research_iteration/"
                    "tool_call в траектории). Ответа без выдумывания нет.")

        answer = self._chat(
            ANSWER_SYSTEM,
            f"ВОПРОС: {question}\n\nИСТОЧНИКИ:\n{_sources_block(sources)}")
        self.ctx.traj.emit("research_iteration", iteration=it, step="answer",
                           chars=len(answer))
        return answer
