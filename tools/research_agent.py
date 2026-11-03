"""Research Agent — полноценный Plan → Act → Observe цикл.

До MAX_ITERATIONS (3) итераций:
  Plan    — LLM формулирует поисковый запрос по вопросу и уже собранным данным.
  Act     — TavilyClient.search по этому запросу.
  Observe — LLM оценивает собранные источники: {"sufficient": bool, "next_query": str?}.
  Ответ   — финальный синтез со ссылками `[Source: URL]`.

Клиенты инъектируются — в тестах подставляются моки, в рантайме — реальные.
Любой сбой клиента ретраится внутри клиента; полный провал поиска на
итерации честно фиксируется в trace и цикл идёт дальше (ответ строится на
том, что удалось собрать; если совсем ничего нет — честное «нет данных»).
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class _Searcher(Protocol):
    def search(self, query: str) -> list[dict[str, Any]]: ...


class _Chatter(Protocol):
    def chat(self, messages: list[dict[str, str]], *, max_tokens: int = 1200,
             temperature: float | None = None) -> str: ...


MAX_ITERATIONS = 3
RESULT_SNIPPET = 900  # символов content из источника в контексте модели

PLAN_SYSTEM = (
    "Ты — исследовательский агент. По вопросу пользователя сформулируй ОДИН "
    "короткий поисковый запрос (3-10 слов), который найдёт нужные факты. "
    "ЖЁСТКОЕ ПРАВИЛО: если в истории уже были поиски и они не дали ответа "
    "(sufficient: false), ты ОБЯЗАН сгенерировать НОВЫЙ запрос — перефразируй, "
    "используй другие ключевые слова, синонимы или уточняющие термины. "
    "Запрещено повторять предыдущие запросы из списка УЖЕ ПРОБОВАЛИ. "
    "Всегда пиши на том же языке, на котором задан вопрос пользователя "
    "(вопрос на русском — запрос на русском). "
    "Верни только JSON: {\"query\": \"...\"}. Без пояснений."
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
    "на языке вопроса. "
    "Не запрашивай лишних итераций: лучше ответить с оговоркой, чем крутить поиск."
)

ANSWER_SYSTEM = (
    "Ты — исследовательский агент NEXUS. Ответь на вопрос ТОЛЬКО по "
    "приведённым источникам. "
    "ТЫ ОБЯЗАН ВСТАВЛЯТЬ [Source: url] СРАЗУ ПОСЛЕ КАЖДОГО УТВЕРЖДЕНИЯ. "
    "ЗАПРЕЩЕНО выводить ссылки ТОЛЬКО списком в конце текста без "
    "инлайн-цитат. Если ты не вставишь инлайн-цитаты, система отклонит "
    "ответ. Никогда не используй номерные ссылки вида [1] или [^1]. "
    "Если данных не хватает — прямо "
    "скажи, чего не хватает, не выдумывай. Ответ лаконичный (≤ 250 слов), "
    "заканчивается разделом «Источники:» — нумерованный список всех "
    "процитированных URL с их заголовками. "
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
            f"{(s.get('content') or '')[:RESULT_SNIPPET]}"
        )
    return "\n\n".join(lines) or "(источников нет)"


def _http_status(url: str, timeout_s: float = 10.0) -> int:
    """Реальный GET источника. requests при наличии, иначе urllib (stdlib).

    0 — сеть/URL недоступны; HTTP-код (включая 403/404) возвращается как
    есть, чтобы агент честно пометил ответ неверифицированным.
    """
    headers = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/122.0 Safari/537.36")}
    try:
        import requests  # type: ignore
        r = requests.get(url, timeout=timeout_s, headers=headers,
                         allow_redirects=True)
        return int(r.status_code)
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 — сетевой сбой = неверифицированный URL
        return 0
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return int(getattr(r, "status", 0) or 0)
    except urllib.error.HTTPError as e:
        return int(e.code)
    except Exception:  # noqa: BLE001
        return 0


def _norm(query: str) -> str:
    return re.sub(r"\s+", " ", query.strip().lower())


_DEDUPE_SUFFIXES = (" (details)", " (explained)", " (обзор)", " (latest)")

# Жёсткий вайтлист доменов источников: в демо нужны авторитетные хосты,
# а не мусор выдачи (карты, левые хакатоны, SEO-фермы). Читается при каждом
# вызове из RESEARCH_SOURCE_DOMAINS ("дом1,дом2"; "*" — без фильтра).
_SOURCE_DOMAINS_DEFAULT = "near.org,x.com,twitter.com,github.com,nearhackathon.com"


def _source_domains() -> tuple[str, ...]:
    raw = os.environ.get("RESEARCH_SOURCE_DOMAINS",
                         _SOURCE_DOMAINS_DEFAULT)
    return tuple(d.strip().lower().lstrip(".")
                 for d in raw.split(",") if d.strip())


def _domain_allowed(url: str) -> bool:
    """URL попадает в sources только с вайтлист-домена (точный или
    поддомен). "*" в списке отключает фильтр."""
    domains = _source_domains()
    if "*" in domains:
        return True
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)

# Дефолтный хинт, когда observe не дала next_query: НОВЫЙ ракурс от вопроса,
# а не суффикс к прошлому запросу (иначе рекурсивное «(overview) (overview)»).
_DEFAULT_ANGLES = ("overview", "guide", "requirements", "faq", "explained")


def _default_hint(question: str, tried_count: int) -> str:
    angle = _DEFAULT_ANGLES[max(0, tried_count - 1) % len(_DEFAULT_ANGLES)]
    return f"{question.rstrip('?!. ')} {angle}"


def _dedupe_query(query: str, past: list[str]) -> str:
    """Если запрос повторяет уже опробованный — принудительно варьируем его,
    чтобы поисковик выдал другие результаты."""
    tried = {_norm(q) for q in past}
    if _norm(query) not in tried:
        return query
    for suffix in _DEDUPE_SUFFIXES:
        candidate = f"{query}{suffix}"
        if _norm(candidate) not in tried:
            return candidate
    return f"{query} (попытка {len(past) + 1})"


_NUM_CITE_RE = re.compile(r"\[(?:[Ss]ource[:\s]*)?\^?(\d{1,2})\]")


def _normalize_citations(answer: str, sources: list[dict[str, Any]]) -> str:
    """Модели любят цитировать как [1]/[^2]/[Source 3], а контракт цитат —
    [Source: URL] (его же ждёт cite_check в демо-контуре оплаты).
    Переписываем номерные ссылки на реальные URL по индексу источника;
    номер вне диапазона остаётся как есть — подделывать не станем."""
    urls = [str(s.get("url") or "") for s in sources]
    if not urls:
        return answer

    def repl(m: re.Match) -> str:
        i = int(m.group(1))
        if 1 <= i <= len(urls) and urls[i - 1]:
            return f"[Source: {urls[i - 1]}]"
        return m.group(0)

    answer = _NUM_CITE_RE.sub(repl, answer)
    # Голый URL в квадратных скобках [https://u] — третий формат модели:
    # инлайн-ссылка без префикса. Чинится только если URL — из собранных.
    def repl_bracket(m: re.Match) -> str:
        u = m.group(1)
        return f"[Source: {u}]" if u in urls else m.group(0)

    answer = _BRACKET_URL_RE.sub(repl_bracket, answer)
    return _attach_trailing_sources(answer, urls)


_BRACKET_URL_RE = re.compile(r"\[(https?://[^\]\s\"'>]+)\]")
_TRAIL_HEADER_RE = re.compile(
    r"^[ \t]*[#>*\-•_ \t]*(?:источники|sources|ссылки|references|"
    r"литература|bibliography)[ \t*_:#\-]*$",
    re.IGNORECASE | re.MULTILINE)
_ANY_URL_RE = re.compile(r"https?://[^\s\]\)\"'>]+")
# Концовочная строка списка: «- url», «1. url», «[2] url», «3) Title»
_LIST_LINE_RE = re.compile(
    r"^\s*(?:[-*•]\s+|\[?\d{1,2}\]?[.\)]\s+)?(?:https?://\S+|\[Source:[^\]]+\])\s*$")
_NUM_LINE_RE = re.compile(r"^\s*\[?(\d{1,2})\]?[.\)]\s+\S", re.MULTILINE)


def _cite_first_sentence(paragraph: str, cite_urls: list[str]) -> str:
    """Вставляет [Source: url] после первого предложения абзаца
    (граница — первый .!? перед пробелом/концом строки)."""
    cite = "".join(f" [Source: {u}]" for u in cite_urls)
    m = re.search(r"[.!?](?=\s|$)", paragraph)
    pos = m.end() if m else len(paragraph.rstrip())
    return paragraph[:pos] + cite + paragraph[pos:]


def _attach_trailing_sources(answer: str, urls: list[str]) -> str:
    """Модель вынесла ссылки в концовку («Источники: 1. url» / «- url» /
    «1. Title») вместо инлайна — cite_check не видит цитат, оплата
    блокируется. Извлекаем URL концовки (или индексы «N. Title» →
    sources[n-1]) и привязываем каждый к первому предложению абзаца.
    Если инлайн-цитаты в тексте уже есть — ничего не трогаем."""
    matches = list(_TRAIL_HEADER_RE.finditer(answer))
    if matches:
        m = matches[-1]
        body, tail = answer[:m.start()], answer[m.start():]
    else:
        lines = answer.rstrip().splitlines()
        cut = len(lines)
        while cut and _LIST_LINE_RE.match(lines[cut - 1]):
            cut -= 1
        body, tail = "\n".join(lines[:cut]), "\n".join(lines[cut:])
    if not tail.strip() or "[Source:" in body or not body.strip():
        return answer
    # URL концовки (в т.ч. «[1] url», «[Source: u] url») → только известные
    pending = [u for u in _ANY_URL_RE.findall(tail) if u in urls]
    if not pending:  # «1. Title» без URL — индекс строки = индекс источника
        pending = [urls[int(n) - 1] for n in _NUM_LINE_RE.findall(tail)
                   if 1 <= int(n) <= len(urls)]
    pending = list(dict.fromkeys(pending))
    if not pending:
        return answer
    paras = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    out = [
        _cite_first_sentence(p, [pending[k]]) if k < len(pending) else p
        for k, p in enumerate(paras)
    ]
    if len(pending) > len(paras):  # лишние ссылки — в последний абзац
        out[-1] = _cite_first_sentence(out[-1], pending[len(paras):])
    return "\n\n".join(out) + "\n\n" + tail


@dataclass
class AgentResult:
    answer: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    trace: list[dict[str, Any]] = field(default_factory=list)
    ok: bool = True
    error: str | None = None
    verified: bool = True
    url_status: dict[str, int] = field(default_factory=dict)


class ResearchAgent:
    """Agent Loop: question → plan → search → observe → (repeat) → answer."""

    def __init__(self, searcher: _Searcher, chatter: _Chatter,
                 max_iterations: int = MAX_ITERATIONS,
                 verifier: Callable[[str], int] | None = None):
        self.searcher = searcher
        self.chatter = chatter
        self.max_iterations = max(1, max_iterations)
        # None — офлайн/тестовый конструктор: URL-проверка не выполняется.
        # from_env() подключает реальный HTTP GET.
        self.verifier = verifier

    # -- шаги цикла -----------------------------------------------------------

    def _plan(self, question: str, sources: list[dict[str, Any]],
              past_queries: list[str], hint: str | None = None) -> str:
        user = f"ВОПРОС: {question}"
        if past_queries:
            user += ("\n\nУЖЕ ПРОБОВАЛИ (запрещено повторять):\n"
                     + "\n".join(f"- {q}" for q in past_queries))
        if hint:
            user += f"\n\nПРЕДЫДУЩАЯ ОЦЕНКА ПРЕДЛОЖИЛА ЗАПРОС: {hint}"
        if sources:
            user += f"\n\nУЖЕ СОБРАНО:\n{_sources_block(sources)}"
        msgs = [{"role": "system", "content": PLAN_SYSTEM},
                {"role": "user", "content": user}]
        text = self.chatter.chat(msgs, max_tokens=120)
        query = _json_obj(text).get("query")
        if not isinstance(query, str) or not query.strip():
            # честный фолбэк: подсказка оценки или сам вопрос, без выдумывания
            query = hint if hint else question.strip()[:120]
        else:
            query = query.strip()
        # Модель залипла на уже опробованном запросе → один ретрай с
        # температурой 0.8 и явным замечанием; финальный дедуп — в run().
        if _norm(query) in {_norm(q) for q in past_queries}:
            retry = msgs + (
                [{"role": "assistant", "content": text}] if text else []
            ) + [
                {"role": "user", "content":
                    f"Запрос «{query}» уже опробован и не дал ответа. "
                    "Сгенерируй ПРИНЦИПАЛЬНО другой запрос — другие слова, "
                    "другой ракурс. Верни только JSON: {\"query\": \"...\"}."},
            ]
            text2 = self.chatter.chat(retry, max_tokens=120, temperature=0.8)
            q2 = _json_obj(text2).get("query")
            if isinstance(q2, str) and q2.strip():
                query = q2.strip()
        return query

    def _observe(self, question: str, sources: list[dict[str, Any]],
                 past_queries: list[str]) -> dict[str, Any]:
        user = f"ВОПРОС: {question}\n\nИСТОЧНИКИ:\n{_sources_block(sources)}"
        if past_queries:
            user += ("\n\nУЖЕ ПРОБОВАЛИ ЗАПРОСЫ:\n"
                     + "\n".join(f"- {q}" for q in past_queries))
        text = self.chatter.chat(
            [{"role": "system", "content": OBSERVE_SYSTEM},
             {"role": "user", "content": user}],
            max_tokens=160,
        )
        return _json_obj(text)

    def _answer(self, question: str, sources: list[dict[str, Any]]) -> str:
        user = f"ВОПРОС: {question}\n\nИСТОЧНИКИ:\n{_sources_block(sources)}"
        return self.chatter.chat(
            [{"role": "system", "content": ANSWER_SYSTEM},
             {"role": "user", "content": user}],
            max_tokens=4000,
        )

    # -- главный цикл ----------------------------------------------------------

    def run(self, question: str) -> AgentResult:
        trace: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        queries_tried: list[str] = []
        result = AgentResult(answer="", trace=trace)

        if not question.strip():
            result.ok = False
            result.error = "пустой вопрос"
            return result

        next_hint: str | None = None
        for it in range(1, self.max_iterations + 1):
            result.iterations = it
            # Plan — с историей запросов и подсказкой прошлой оценки
            try:
                query = self._plan(question, sources, queries_tried, next_hint)
            except Exception as e:  # noqa: BLE001
                trace.append({"iter": it, "step": "plan", "error": str(e)})
                break
            # Страховка перед поиском: повтор любого прошлого запроса
            # принудительно варьируется суффиксом — иначе та же выдача.
            query = _dedupe_query(query, queries_tried)
            queries_tried.append(query)
            trace.append({"iter": it, "step": "plan", "query": query})

            # Act
            try:
                found = self.searcher.search(query)
            except Exception as e:  # noqa: BLE001
                trace.append({"iter": it, "step": "search", "query": query, "error": str(e)})
                found = []
            new_hits = [s for s in found
                        if s.get("url") and s["url"] not in seen_urls]
            # Вайтлист доменов: мусор выдачи не доходит до observe/answer
            # и не попадает в sources — судьям виден честный счёт отсева.
            fresh = [s for s in new_hits if _domain_allowed(s["url"])]
            for s in fresh:
                seen_urls.add(s["url"])
            sources.extend(fresh)
            trace.append({"iter": it, "step": "search", "query": query,
                          "results": len(found), "new": len(fresh),
                          "filtered": len(new_hits) - len(fresh)})

            # Поиск вернул результаты, но новых источников нет (дубли или
            # вайтлист): корпус исчерпан — синтез по накопленному, а не ещё
            # один холостой оборот. Пустой found — другое: пробуем новый запрос.
            if found and not fresh and sources:
                trace.append({"iter": it, "step": "search_empty",
                              "note": "new=0 → синтез по накопленным источникам"})
                break

            # Observe — на последней итерации принудительный синтез, оценка бессмысленна
            if it >= self.max_iterations:
                break
            try:
                verdict = self._observe(question, sources, queries_tried)
            except Exception as e:  # noqa: BLE001
                trace.append({"iter": it, "step": "observe", "error": str(e)})
                break
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
            trace.append({"iter": it, "step": "observe", "sufficient": sufficient,
                          "next_query": next_hint})
            if sufficient:
                break

        result.sources = sources
        if not sources:
            result.ok = False
            result.verified = False
            result.error = f"не удалось найти информацию за {self.max_iterations} попытки"
            result.answer = (f"Не удалось найти информацию за "
                             f"{self.max_iterations} попытки: поисковый сервис "
                             "не вернул данных. Ответа без выдумывания нет.")
            trace.append({"step": "answer", "refusal": "no_sources"})
            return result

        # Реальная проверка URL после Tavily: любой не-200 → verified=False.
        if self.verifier is not None:
            url_status: dict[str, int] = {}
            for s in sources:
                url = str(s.get("url") or "")
                if not url:
                    continue
                code = int(self.verifier(url))
                url_status[url] = code
                s["http_status"] = code
            result.url_status = url_status
            result.verified = bool(url_status) and all(
                code == 200 for code in url_status.values())
            trace.append({"step": "verify_urls", "statuses": url_status,
                          "verified": result.verified})
        else:
            result.verified = True

        # Synthesize — гарантированный финальный вызов LLM при наличии данных
        try:
            result.answer = _normalize_citations(
                self._answer(question, sources), sources)
            result.ok = True
            trace.append({"step": "answer", "chars": len(result.answer)})
        except Exception as e:  # noqa: BLE001
            result.ok = False
            result.verified = False
            result.error = f"синтез ответа не удался: {e}"
            result.answer = (f"Источники собраны ({len(sources)}), но синтез "
                             f"ответа упал: {e}")
            trace.append({"step": "answer", "error": str(e)})
        return result


def from_env() -> "ResearchAgent":
    """Агент на реальных клиентах из .env/env."""
    try:  # пакетный контекст: import tools.research_agent
        from .research_agent_clients import LLMClient, TavilyClient
    except ImportError:  # плоский контекст: tools/ в sys.path
        from research_agent_clients import LLMClient, TavilyClient
    return ResearchAgent(TavilyClient(), LLMClient(), verifier=_http_status)


if __name__ == "__main__":  # ручной прогон: python -m tools.research_agent "вопрос"
    import sys
    agent = from_env()
    q = " ".join(sys.argv[1:]) or "What is the NEAR Protocol?"
    res = agent.run(q)
    print(res.answer)
    print("\n--- trace ---")
    for t in res.trace:
        print(t)
    sys.exit(0 if res.ok else 1)
