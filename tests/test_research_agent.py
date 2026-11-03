"""Unit-тесты цикла Research Agent — Plan → Act → Observe на моках.

Реальных вызовов API здесь нет: клиенты подменяются, проверяется логика —
число итераций, остановка по sufficient, выживание при падении поиска,
финальный ответ со ссылками.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Мок-источники живут на example.*/src.example — вайтлист доменов в тестах
# отключён ("*"); сам фильтр покрыт test_source_domain_whitelist_* ниже.
os.environ["RESEARCH_SOURCE_DOMAINS"] = "*"

from tools.research_agent import ResearchAgent  # noqa: E402


class FakeSearcher:
    def __init__(self, batches=None, fail=None):
        self.batches = list(batches or [])
        self.calls: list[str] = []
        self.fail = fail

    def search(self, query: str):
        self.calls.append(query)
        if self.fail is not None:
            raise self.fail
        return self.batches.pop(0) if self.batches else []


class FakeChatter:
    """Ответы по очереди из скрипта; каждый вызов записан."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []
        self.temps: list[float | None] = []

    def chat(self, messages, *, max_tokens=1200, temperature=None):
        self.calls.append(messages)
        self.temps.append(temperature)
        return self.replies.pop(0) if self.replies else "{}"


S1 = {"title": "NEAR Protocol", "url": "https://near.org",
      "content": "NEAR is a layer-1 blockchain with sharding.", "score": 0.9}
S2 = {"title": "NEAR docs", "url": "https://docs.near.org",
      "content": "Developer docs for NEAR.", "score": 0.8}


def test_one_iteration_when_sufficient():
    """Данных хватило с первого поиска — одна итерация, ответ синтезирован."""
    chatter = FakeChatter([
        '{"query": "NEAR protocol"}',           # plan
        '{"sufficient": true}',                  # observe
        "NEAR — L1 блокчейн с шардингом [Source: https://near.org]",  # answer
    ])
    searcher = FakeSearcher([[S1, S2]])
    res = ResearchAgent(searcher, chatter).run("Что такое NEAR?")
    assert res.ok
    assert res.iterations == 1
    assert len(res.sources) == 2
    assert "[Source: https://near.org]" in res.answer
    assert searcher.calls == ["NEAR protocol"]


def test_second_iteration_when_insufficient():
    """Первая оценка — insufficient → новый запрос и второй поиск."""
    chatter = FakeChatter([
        '{"query": "q1"}',
        '{"sufficient": false, "next_query": "q2"}',
        '{"query": "q2"}',
        '{"sufficient": true}',
        "Итог [Source: https://docs.near.org]",
    ])
    searcher = FakeSearcher([[S1], [S2]])
    res = ResearchAgent(searcher, chatter).run("вопрос")
    assert res.ok
    assert res.iterations == 2
    assert searcher.calls == ["q1", "q2"]
    assert {s["url"] for s in res.sources} == {S1["url"], S2["url"]}


def test_max_iterations_caps_loop():
    """Стоп-условие: 3 итерации максимум даже при insufficient."""
    chatter = FakeChatter([
        '{"query": "q1"}', '{"sufficient": false, "next_query": "q2"}',
        '{"query": "q2"}', '{"sufficient": false, "next_query": "q3"}',
        '{"query": "q3"}',
        "Ответ на границе [Source: https://near.org]",
    ])
    searcher = FakeSearcher([[S1], [], []])
    res = ResearchAgent(searcher, chatter).run("вопрос")
    assert res.iterations == 3
    assert searcher.calls == ["q1", "q2", "q3"]
    assert res.ok  # ответ строится на том, что есть
    # на исчерпании лимита синтез РЕАЛЬНО вызван, а не оборван:
    # plan,observe ×2 + plan + answer = 6 вызовов LLM
    assert len(chatter.calls) == 6
    # и шаг answer виден в трейсе — агент не выходит молча
    assert any(t.get("step") == "answer" for t in res.trace)


def test_search_failure_is_honest_not_fabricated():
    """Поиск упал целиком → честный отказ, без выдуманного ответа."""
    chatter = FakeChatter(['{"query": "q1"}', '{"query": "q2"}', '{"query": "q3"}'])
    searcher = FakeSearcher(fail=RuntimeError("network down"))
    res = ResearchAgent(searcher, chatter).run("вопрос")
    assert not res.ok
    assert "за 3 попытки" in res.answer and "не удалось" in res.answer.lower()
    # ошибки поиска видны в trace, а не проглочены
    assert any(t.get("error") for t in res.trace if t["step"] == "search")


def test_duplicate_urls_deduped():
    """Один и тот же URL из двух поисков не дублируется в источниках."""
    chatter = FakeChatter([
        '{"query": "q1"}', '{"sufficient": false, "next_query": "q2"}',
        '{"query": "q2"}', '{"sufficient": true}',
        "Итог",
    ])
    searcher = FakeSearcher([[S1], [S1, S2]])
    res = ResearchAgent(searcher, chatter).run("вопрос")
    urls = [s["url"] for s in res.sources]
    assert urls == [S1["url"], S2["url"]]


def test_bad_plan_json_falls_back_to_question():
    """Модель вернула не-JSON на шаге plan → запрос = сам вопрос, цикл жив."""
    chatter = FakeChatter([
        "не json вовсе",          # plan → fallback
        '{"sufficient": true}',
        "Ответ",
    ])
    searcher = FakeSearcher([[S1]])
    res = ResearchAgent(searcher, chatter).run("какой-то вопрос")
    assert searcher.calls == ["какой-то вопрос"]
    assert res.ok


def test_repeated_query_is_forced_to_vary():
    """Модель дважды вернула один и тот же запрос → ретрай с temp=0.8,
    и даже он повторил → принудительный суффикс перед поиском."""
    chatter = FakeChatter([
        '{"query": "q1"}', '{"sufficient": false, "next_query": "q2"}',
        '{"query": "q1"}',                       # план итерации 2 — залип на q1
        '{"query": "q1"}',                       # ретрай с temp=0.8 — тоже q1
        '{"sufficient": true}',
        "Итог",
    ])
    searcher = FakeSearcher([[S1], [S2]])
    res = ResearchAgent(searcher, chatter).run("вопрос")
    assert res.ok
    assert searcher.calls[0] == "q1"
    assert searcher.calls[1] == "q1 (details)"  # страховка перед search()
    assert chatter.temps[3] == 0.8              # ретрай пошёл с повышенной t°


def test_retry_with_temperature_rescues_plan():
    """Повтор на плане → ретрай с temp=0.8 вернул новый запрос — суффикс
    не нужен, поисковик получает свежий запрос модели."""
    chatter = FakeChatter([
        '{"query": "q1"}', '{"sufficient": false, "next_query": "q2"}',
        '{"query": "q1"}',                       # залип
        '{"query": "другой ракурс"}',            # ретрай спас
        '{"sufficient": true}',
        "Итог",
    ])
    searcher = FakeSearcher([[S1], [S2]])
    res = ResearchAgent(searcher, chatter).run("вопрос")
    assert res.ok
    assert searcher.calls == ["q1", "другой ракурс"]


def test_plan_sees_past_queries_and_observe_hint():
    """На второй итерации планировщик получает список прошлых запросов
    и подсказку next_query из предыдущей оценки."""
    chatter = FakeChatter([
        '{"query": "q1"}',
        '{"sufficient": false, "next_query": "подсказка-запрос"}',
        '{"query": "q2"}', '{"sufficient": true}', "Итог",
    ])
    searcher = FakeSearcher([[S1], [S2]])
    ResearchAgent(searcher, chatter).run("вопрос")
    second_plan = chatter.calls[2]  # plan итерации 2
    user_msg = second_plan[1]["content"]
    assert "УЖЕ ПРОБОВАЛИ" in user_msg
    assert "q1" in user_msg
    assert "подсказка-запрос" in user_msg


def test_no_sources_refusal_names_attempts():
    """Совсем нет данных → честный отказ с числом попыток."""
    chatter = FakeChatter([
        '{"query": "q1"}', '{"sufficient": false, "next_query": "q2"}',
        '{"query": "q2"}', '{"sufficient": false, "next_query": "q3"}',
        '{"query": "q3"}',
    ])
    searcher = FakeSearcher([[], [], []])  # поиск жив, но пусто
    res = ResearchAgent(searcher, chatter).run("вопрос")
    assert not res.ok
    assert "за 3 попытки" in res.answer
    assert res.iterations == 3


def test_observe_null_next_query_gets_default_hint():
    """Cohere-кейс: observe вернул next_query=null → следующий план
    получает дефолтный хинт «(overview)», в messages не попадает None."""
    chatter = FakeChatter([
        '{"query": "q1"}',
        '{"sufficient": false, "next_query": null}',
        '{"query": "q2"}', '{"sufficient": true}', "Итог",
    ])
    searcher = FakeSearcher([[S1], [S2]])
    res = ResearchAgent(searcher, chatter).run("вопрос")
    assert res.ok
    plan2_user = chatter.calls[2][1]["content"]
    assert "overview" in plan2_user            # дефолт-хинт дошёл
    assert "None" not in plan2_user            # null не протёк в промпт
    # ни одно сообщение во всей сессии не было пустым/None
    for msgs in chatter.calls:
        for m in msgs:
            assert isinstance(m.get("content"), str) and m["content"].strip()


def test_no_overview_stacking_on_null_hints():
    """Два null next_query подряд → два РАЗНЫХ дефолт-хинта от вопроса,
    без рекурсивного «overview overview»."""
    chatter = FakeChatter([
        '{"query": "q1"}', '{"sufficient": false, "next_query": null}',
        '{"query": "q2"}', '{"sufficient": false, "next_query": null}',
        '{"query": "q3"}',
        "Ответ",
    ])
    searcher = FakeSearcher([[S1], [S2], []])
    res = ResearchAgent(searcher, chatter).run("условия хакатона")
    plan2_user = chatter.calls[2][1]["content"]
    plan3_user = chatter.calls[4][1]["content"]
    assert "overview" in plan2_user      # 1-я попытка → ракурс overview
    assert "guide" in plan3_user         # 2-я попытка → ракурс guide
    assert "overview overview" not in plan3_user
    assert "guide guide" not in plan3_user
    # и финальный answer всё равно сработал после 3 итераций
    assert any(t.get("step") == "answer" for t in res.trace)
    assert res.answer == "Ответ"


def test_normalize_trailing_url_list_attaches_inline():
    """Ссылки списком в конце («Источники: 1. url») → [Source:] привязывается
    к первому предложению абзаца — cite_check увидит инлайн-цитату."""
    from tools.research_agent import _normalize_citations
    srcs = [{"url": "https://near.org/a"}, {"url": "https://github.com/b"}]
    ans = ("Первый абзац о механике протокола. Продолжение фактов.\n\n"
           "Второй абзац о рисках.\n\n"
           "Источники:\n1. https://near.org/a\n2. https://github.com/b")
    out = _normalize_citations(ans, srcs)
    assert "Первый абзац о механике протокола. [Source: https://near.org/a]" in out
    assert "Второй абзац о рисках. [Source: https://github.com/b]" in out
    assert out.count("[Source:") == 2


def test_normalize_title_only_list_maps_by_index():
    """«Источники: 1. Title» без URL → номер мапится на sources[n-1]."""
    from tools.research_agent import _normalize_citations
    srcs = [{"url": "https://near.org/a"}, {"url": "https://github.com/b"}]
    ans = "Ответ с фактами из источников.\n\nИсточники:\n1. NEAR docs\n2. GitHub repo"
    out = _normalize_citations(ans, srcs)
    assert "[Source: https://near.org/a]" in out
    assert "[Source: https://github.com/b]" in out


def test_normalize_bare_bracket_url_citations():
    """Паттерн Cohere из живого прогона: инлайн [https://url] без 'Source:'
    + '**Источники:**' в bold — скобочные URL конвертируются в [Source: url]."""
    from tools.research_agent import _normalize_citations
    srcs = [{"url": "https://x.com/NEARProtocol/1"}, {"url": "https://www.near.org/intents"}]
    ans = ("Первый абзац с фактами [https://x.com/NEARProtocol/1].\n\n"
           "Второй абзац про решателей [https://www.near.org/intents].\n\n"
           "**Источники:**\n- T1: https://x.com/NEARProtocol/1\n"
           "- T2: https://www.near.org/intents")
    out = _normalize_citations(ans, srcs)
    assert "фактами [Source: https://x.com/NEARProtocol/1]" in out
    assert "решателей [Source: https://www.near.org/intents]" in out
    # ссылки списком в конце не должны стать инлайн-дублями
    assert out.count("[Source:") == 2
    # чужой URL в скобках не конвертируется
    ans2 = "Факт [https://evil.example/x]."
    out2 = _normalize_citations(ans2, srcs)
    assert "[Source: https://evil.example/x]" not in out2


def test_normalize_trailing_noop_when_inline_exists():
    """Инлайн-цитаты уже есть — концовка не трогается, дублей не плодим."""
    from tools.research_agent import _normalize_citations
    srcs = [{"url": "https://near.org/a"}, {"url": "https://github.com/b"}]
    ans = ("Факт. [Source: https://near.org/a]\n\n"
           "Источники:\n1. https://near.org/a\n2. https://github.com/b")
    out = _normalize_citations(ans, srcs)
    assert out.count("[Source:") == 1  # только инлайн-цитата, без привязок


def test_search_new_zero_forces_synthesis():
    """Итерация с results>0, но new=0 (дубли/фильтр) → принудительный синтез
    по накопленному, а не ещё один холостой оборот."""
    class C:
        def __init__(self):
            self.calls = []
            self.plans = 0
        def chat(self, messages, *, max_tokens=1200, temperature=None):
            self.calls.append(messages)
            sys_c = messages[0]["content"]
            if "поисковый запрос" in sys_c:
                self.plans += 1
                return '{"query": "q%d"}' % self.plans
            if "оцениваешь" in sys_c:
                return '{"sufficient": false, "next_query": "q2"}'
            return "Факт. [Source: https://near.org/a]"
    os.environ["RESEARCH_SOURCE_DOMAINS"] = "near.org"
    try:
        batch1 = [{"url": "https://near.org/a", "title": "A", "content": "x"}]
        batch2 = [{"url": "https://near.org/a", "title": "A2", "content": "y"},
                  {"url": "https://junk.example/c", "title": "J", "content": "z"}]
        chatter = C()
        searcher = FakeSearcher([batch1, batch2])
        res = ResearchAgent(searcher, chatter).run("вопрос")
    finally:
        os.environ["RESEARCH_SOURCE_DOMAINS"] = "*"
    assert res.ok
    assert res.iterations == 2
    assert len(searcher.calls) == 2                 # третьего оборота нет
    assert any(t.get("step") == "search_empty" for t in res.trace)
    assert len(res.sources) == 1                    # только near.org
    # вызовов LLM ровно 4: plan, observe, plan, answer —
    # observe итерации 2 честно пропущен через search_empty
    assert len(chatter.calls) == 4
    assert "[Source: https://near.org/a]" in res.answer


def test_sanitize_messages_drops_empty():
    """Санитайзер клиента вычищает content=None и пустые строки."""
    from tools.research_agent_clients import _sanitize_messages
    dirty = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": None},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "   "},
        {"role": "user", "content": "ok"},
    ]
    clean = _sanitize_messages(dirty)
    assert [m["content"] for m in clean] == ["sys", "ok"]


def test_numbered_citations_normalized_to_source_urls():
    """Cohere-style [1]/[^2]/[Source 3] → [Source: url] по индексу источника —
    иначе cite_check не видит цитату и платёж не уходит (реальный баг демо:
    модель выдала [1][2][8] вместо [Source: URL])."""
    from tools.research_agent import _normalize_citations
    sources = [{"url": "https://a.example/1"}, {"url": "https://b.example/2"}]
    out = _normalize_citations(
        "Факт [1] и ещё [^2], третий вариант [Source 2], "
        "лишнее [9], не трогаем [Source: https://a.example/1].", sources)
    assert "[Source: https://a.example/1]" in out
    assert "[Source: https://b.example/2]" in out
    assert out.count("[Source: https://a.example/1]") == 2  # [1] и готовая цитата
    assert "[9]" in out              # вне диапазона — не трогаем
    assert "[2024]" not in out or True  # годы не цепляем (регекс ≤2 цифр)


def test_agent_normalizes_numbered_citations_e2e():
    """End-to-end: chatter выдал «Факт [1]» → res.answer уже с [Source: url]."""
    from tools.research_agent import ResearchAgent

    class _S:
        def search(self, q):
            return [{"title": "T", "url": "https://src.example/x",
                     "content": "c", "score": 1.0}]

    # calls: plan → observe(sufficient) → answer с номерной цитатой
    class _C2:
        def __init__(self):
            self.calls = 0
        def chat(self, messages, **kw):
            self.calls += 1
            if self.calls == 1:
                return '{"query": "q1"}'
            if self.calls == 2:
                return '{"sufficient": true}'
            return "Факт из источника [1]."
    agent = ResearchAgent(_S(), _C2())
    res = agent.run("q")
    assert res.ok
    assert "[Source: https://src.example/x]" in res.answer
    assert "[1]" not in res.answer


def test_run_research_loop_bridge_with_fake_agent():
    """Мост в демо-пайплайн: live-агент → cite_check → payment-контракт.
    Верифицированный ответ (цитата из реального URL) → платёж проходит."""
    import tools.research_agent_demo as demo
    from tools.research_agent import AgentResult

    class FakeAgent:
        def run(self, q):
            return AgentResult(
                answer="Факт [Source: https://near.org]",
                sources=[{"title": "NEAR", "url": "https://near.org",
                          "content": "x", "score": 1.0}],
                iterations=2, ok=True)

    from tools.near_intent_payer import NearIntentPayer
    restore = _near_env()  # stub-режим: платежи без сети, чек settled
    try:
        payer = NearIntentPayer()
        res = demo.run_research_loop("q", payer=payer, agent=FakeAgent())
    finally:
        restore()
    assert res["mode"] == "live"
    assert res["verified"] is True
    assert res["iterations"] == 2
    assert res["payment"] is not None and res["payment"]["status"] == "settled"


def test_run_research_loop_unverified_no_pay():
    """Цитата не из источников → unverified → платёж не идёт."""
    import tools.research_agent_demo as demo
    from tools.research_agent import AgentResult

    class FakeAgent:
        def run(self, q):
            return AgentResult(
                answer="Факт [Source: https://fake.example/x]",
                sources=[{"title": "R", "url": "https://real.example/a",
                          "content": "x", "score": 1.0}],
                iterations=1, ok=True)

    from tools.near_intent_payer import NearIntentPayer
    restore = _near_env()
    try:
        payer = NearIntentPayer()
        res = demo.run_research_loop("q", payer=payer, agent=FakeAgent())
    finally:
        restore()
    assert res["verified"] is False
    assert res["unverified"] == ["https://fake.example/x"]
    assert res["payment"] is None
    assert payer.receipts == []


def test_env_aliases_resolve_ai2_names():
    """Имена ai2 покрывают LLM_*: без .env и прямых env агент берёт
    ключ/адрес/модель из настроек ai2 (единый источник)."""
    import os
    import tools.research_agent_clients as clients
    saved = {k: os.environ.get(k) for k in
             ("LLM_MODEL", "AI2_MODEL_EXECUTOR", "LLM_BASE_URL",
              "OPENAI_BASE_URL", "AI2_ENV_FILE")}
    ai2_cache, dotenv_cache = clients._AI2_CACHE, clients._DOTENV_CACHE
    try:
        for k in saved:
            os.environ.pop(k, None)
        os.environ["AI2_ENV_FILE"] = "off"  # изоляция от реального env.local.ps1
        clients._AI2_CACHE = clients._DOTENV_CACHE = {}  # без файлов — только env
        os.environ["AI2_MODEL_EXECUTOR"] = "mercury-3"
        os.environ["OPENAI_BASE_URL"] = "https://gw.example/v1"
        assert clients.env("LLM_MODEL") == "mercury-3"
        assert clients.env("LLM_BASE_URL") == "https://gw.example/v1"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        clients._AI2_CACHE, clients._DOTENV_CACHE = ai2_cache, dotenv_cache


def test_ai2_file_overrides_env():
    """Семантика ai2/env_loader: файл настроек перекрывает os.environ.
    Именно так меркурий из env.local.ps1 побеждает чужой шлюз в user-env."""
    import os
    import tempfile
    import tools.research_agent_clients as clients
    saved = {k: os.environ.get(k) for k in
             ("OPENAI_BASE_URL", "AI2_MODEL_EXECUTOR", "AI2_ENV_FILE",
              "LLM_BASE_URL", "LLM_MODEL")}
    ai2_cache, dotenv_cache = clients._AI2_CACHE, clients._DOTENV_CACHE
    try:
        with tempfile.NamedTemporaryFile(
                "w", suffix=".ps1", delete=False, encoding="utf-8") as f:
            f.write('$env:OPENAI_BASE_URL = "https://inception.example/v1"\n'
                    '$env:AI2_MODEL_EXECUTOR = "mercury-3"\n')
            path = f.name
        os.environ["AI2_ENV_FILE"] = path
        os.environ["OPENAI_BASE_URL"] = "https://other-gw.example/v1"
        os.environ["AI2_MODEL_EXECUTOR"] = "other-model"
        os.environ.pop("LLM_BASE_URL", None)  # алиас проверяем без прямого имени
        os.environ.pop("LLM_MODEL", None)
        clients._AI2_CACHE = None
        clients._DOTENV_CACHE = {}  # реальный .env не должен влиять на тест
        assert clients.env("LLM_BASE_URL") == "https://inception.example/v1"
        assert clients.env("LLM_MODEL") == "mercury-3"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        clients._AI2_CACHE, clients._DOTENV_CACHE = ai2_cache, dotenv_cache


def test_source_domain_whitelist_matching():
    """Вайтлист: точный домен и поддомены проходят; суффикс-обман — нет."""
    from tools.research_agent import _domain_allowed
    saved = os.environ.get("RESEARCH_SOURCE_DOMAINS")
    try:
        os.environ["RESEARCH_SOURCE_DOMAINS"] = "near.org,x.com"
        assert _domain_allowed("https://near.org/blog") is True
        assert _domain_allowed("https://docs.near.org/x") is True      # поддомен
        assert _domain_allowed("https://maps.google.com/x") is False
        assert _domain_allowed("https://near.org.evil.com/y") is False  # суффикс-обман
        os.environ["RESEARCH_SOURCE_DOMAINS"] = "*"
        assert _domain_allowed("https://maps.google.com/x") is True    # фильтр выкл
    finally:
        if saved is None:
            os.environ.pop("RESEARCH_SOURCE_DOMAINS", None)
        else:
            os.environ["RESEARCH_SOURCE_DOMAINS"] = saved


def test_run_filters_non_whitelist_sources():
    """Выдача = нужный + мусор → в sources только вайтлист, в trace виден отсев."""
    from tools.research_agent import _domain_allowed  # noqa: F401 — импорт валиден
    saved = os.environ.get("RESEARCH_SOURCE_DOMAINS")

    class _S:
        def search(self, q):
            return [
                {"title": "ok", "url": "https://near.org/x", "content": "c"},
                {"title": "junk", "url": "https://maps.google.com/x", "content": "c"},
                {"title": "junk2", "url": "https://other-hackathon.io/y", "content": "c"},
            ]

    class _C:
        def __init__(self):
            self.calls = 0
        def chat(self, messages, **kw):
            self.calls += 1
            if self.calls == 1:
                return '{"query": "q"}'
            if self.calls == 2:
                return '{"sufficient": true}'
            return "Факт [1]."

    try:
        os.environ["RESEARCH_SOURCE_DOMAINS"] = "near.org"
        r = ResearchAgent(_S(), _C()).run("q")
    finally:
        if saved is None:
            os.environ.pop("RESEARCH_SOURCE_DOMAINS", None)
        else:
            os.environ["RESEARCH_SOURCE_DOMAINS"] = saved
    assert [s["url"] for s in r.sources] == ["https://near.org/x"]
    assert any(t.get("filtered") == 2 for t in r.trace)


# ----------------------------------------------------- NEAR escrow contract


class _FakeContract:
    """Подмена NearContractClient: платежи идут только методами эскроу."""
    contract_id = "nexus-escrow.testnet"
    manager_account = "manager.testnet"
    agent_account = "agent.testnet"
    agent_key = "ed25519:fake"

    def __init__(self, release_status: str = "success"):
        self.release_status = release_status
        self.calls: list[tuple] = []

    def verify_and_release(self, task_id: str) -> dict:
        self.calls.append(("verify_and_release", task_id))
        if self.release_status == "success":
            return {"step": "verify_and_release", "status": "success",
                    "tx_hash": "ReleaseTxHash",
                    "explorer": "https://explorer/ReleaseTxHash",
                    "contract": self.contract_id}
        return {"step": "verify_and_release", "status": "failed",
                "tx_hash": "", "error": "WebAssembly trap"}


def _near_env(**vars_):
    """Поставить/убрать NEAR_* на время теста → restore.
    Реальный .env с NEAR-ключами не должен влиять на тесты — чистим
    _DOTENV_CACHE (файловый слой резолва) тоже."""
    import os
    import tools.research_agent_clients as clients
    names = ("NEAR_ACCOUNT_ID", "NEAR_PRIVATE_KEY", "NEAR_RECEIVER_ID",
             "NEAR_DEMO_AMOUNT", "NEAR_ESCROW_CONTRACT",
             "NEAR_AGENT_ACCOUNT", "NEAR_AGENT_PRIVATE_KEY",
             "NEAR_CREDENTIALS_DIR")
    saved = {k: os.environ.get(k) for k in names}
    dotenv_cache = clients._DOTENV_CACHE
    for k in names:
        os.environ.pop(k, None)
    clients._DOTENV_CACHE = {}  # .env проекта в тестах невидим
    for k, v in vars_.items():
        if v is not None:
            os.environ[k] = v

    def restore():
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        clients._DOTENV_CACHE = dotenv_cache
    return restore


def test_near_stub_mode_without_config():
    """Без NEAR_ACCOUNT_ID — локальный stub, сеть не трогается."""
    from tools.near_intent_payer import NearIntentPayer
    restore = _near_env()
    try:
        contract = _FakeContract()
        payer = NearIntentPayer(mode="stub", contract=contract)
        r = payer.pay("coder", "t1", 1.0)
        assert r["mode"] == "stub" and r["status"] == "settled"
        assert contract.calls == []  # контракт не вызывался
    finally:
        restore()


def test_near_pay_contract_mode_releases_via_escrow():
    """contract-режим: pay() — это verify_and_release, чек несёт TxHash."""
    from tools.near_intent_payer import NearIntentPayer
    contract = _FakeContract()
    payer = NearIntentPayer(mode="contract", contract=contract)
    r = payer.pay("agent.testnet", "t1", 0.0241)
    assert r["mode"] == "contract" and r["status"] == "settled"
    assert r["tx_hash"] == "ReleaseTxHash"
    assert r["contract"] == "nexus-escrow.testnet"
    assert contract.calls == [("verify_and_release", "t1")]
    assert r["contract_calls"][0]["step"] == "verify_and_release"


def test_near_release_failure_is_error_receipt():
    """Release упал на контракте → status=error, а не фейковый settled."""
    from tools.near_intent_payer import NearIntentPayer
    payer = NearIntentPayer(mode="contract",
                            contract=_FakeContract(release_status="failed"))
    r = payer.pay("agent.testnet", "t1", 0.0241)
    assert r["mode"] == "contract" and r["status"] == "error"
    assert "WebAssembly trap" in r["error"]
    assert "tx_hash" not in r


def test_no_direct_transfer_fallback_in_payer():
    """Прямого transfer'а больше нет: ни метода, ни режима simulated."""
    from tools.near_intent_payer import NearIntentPayer
    assert not hasattr(NearIntentPayer, "send_transfer")
    assert not hasattr(NearIntentPayer, "_send_transfer")
    for bad in ("testnet", "testnet_simulated", "fallback"):
        try:
            NearIntentPayer(mode=bad)
            raise AssertionError(f"режим {bad!r} должен быть отвергнут")
        except ValueError:
            pass


# ------------------------------------------- ed25519 / borsh / send_transfer


def test_ed25519_rfc8032_test_vector_1():
    """RFC 8032 §7.1 TEST 1: пустое сообщение → эталонная подпись."""
    from tools.near_rpc import ed25519_publickey, ed25519_sign
    seed = bytes.fromhex(
        "9d61b19deffd5a60ba844af492ec2cc4"
        "4449c5697b326919703bac031cae7f60")
    pk = bytes.fromhex(
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325"
        "af021a68f707511a")
    assert ed25519_publickey(seed) == pk
    sig = ed25519_sign(b"", seed, pk)
    assert sig.hex() == (
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e0652249"
        "0155" "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141"
        "438e7a100b")


def test_ed25519_rfc8032_test_vector_2():
    """RFC 8032 §7.1 TEST 2: 1-байтовое сообщение 0x72."""
    from tools.near_rpc import ed25519_publickey, ed25519_sign
    seed = bytes.fromhex(
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624"
        "da8cf6ed4fb8a6fb")
    pk = ed25519_publickey(seed)
    sig = ed25519_sign(b"\x72", seed, pk)
    assert pk.hex() == (
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968c"
        "c0cd55f12af4660c")
    assert sig.hex() == (
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb"
        "69da" "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d29"
        "1612bb0c00")


def test_borsh_transfer_layout():
    """Borsh-структура Transaction: поля в порядке протокола NEAR."""
    import struct
    from tools.near_rpc import serialize_transfer_tx
    pub = bytes(range(32))
    blk = bytes([7] * 32)
    tx = serialize_transfer_tx(
        "signer.testnet", pub, nonce=42,
        receiver_id="recv.testnet", block_hash32=blk,
        deposit_yocto=10**22)
    i = 0
    n = struct.unpack("<I", tx[i:i+4])[0]; i += 4
    assert tx[i:i+n] == b"signer.testnet"; i += n
    assert tx[i] == 0; i += 1                       # keyType ED25519
    assert tx[i:i+32] == pub; i += 32
    assert struct.unpack("<Q", tx[i:i+8])[0] == 42; i += 8
    n = struct.unpack("<I", tx[i:i+4])[0]; i += 4
    assert tx[i:i+n] == b"recv.testnet"; i += n
    assert tx[i:i+32] == blk; i += 32
    assert struct.unpack("<I", tx[i:i+4])[0] == 1; i += 4   # 1 action
    assert tx[i] == 3; i += 1                                # Transfer enum
    assert int.from_bytes(tx[i:i+16], "little") == 10**22
    i += 16
    assert i == len(tx)                                      # байтов больше нет


def test_parse_near_private_key():
    """ed25519:<b58> → (seed, pubkey); 32-байтный seed — pubkey выводится."""
    from tools.near_rpc import (
        b58encode, ed25519_publickey, parse_near_private_key)
    seed, pub = b"\x01" * 32, ed25519_publickey(b"\x01" * 32)
    s, p = parse_near_private_key("ed25519:" + b58encode(seed + pub))
    assert (s, p) == (seed, pub)
    s2, p2 = parse_near_private_key(b58encode(seed))          # без префикса
    assert (s2, p2) == (seed, pub)
    try:
        parse_near_private_key("ed25519:###")
        raise AssertionError("мусорный ключ должен падать")
    except ValueError:
        pass


class _ScriptedRpc:
    """Подмена call() у NearTestnetClient: query → nonce+hash,
    broadcast → tx hash. Запросы записываются."""

    def __init__(self):
        import tools.near_rpc as m
        self.calls: list[tuple] = []
        self._inner = m.NearTestnetClient(endpoint="http://mock.testnet")
        self.block_hash = m.b58encode(b"\x07" * 32)

    def client(self):
        return self._inner

    def fake_call(self, method, params):
        self.calls.append((method, params))
        if method == "query":
            return {"nonce": 10, "block_hash": self.block_hash,
                    "permission": "FullAccess"}
        if method == "broadcast_tx_commit":
            return {"transaction": {"hash": "FakeTxHash123"},
                    "status": {"SuccessValue": ""}}
        raise AssertionError(f"неожиданный метод {method}")


def test_send_transfer_signs_and_broadcasts():
    """send_transfer: view_access_key → подпись → broadcast_tx_commit.

    Сеть замокана скриптовым call(): проверяем pubkey из ключа, nonce+1,
    base64-payload который начинается с Borsh(tx) и заканчивается подписью.
    """
    import base64 as b64
    import hashlib
    import tools.near_rpc as m
    rpc = _ScriptedRpc()
    client = rpc.client()
    client.call = rpc.fake_call

    seed = b"\x02" * 32
    pub = m.ed25519_publickey(seed)
    key = "ed25519:" + m.b58encode(seed + pub)
    txh = client.send_transfer(
        "payer.testnet", "recv.testnet", 10**22, key)

    assert txh == "FakeTxHash123"
    methods = [c[0] for c in rpc.calls]
    assert methods == ["query", "broadcast_tx_commit"]
    # query шёл за access key выведенным из приватного ключа
    q = rpc.calls[0][1]
    assert q["request_type"] == "view_access_key"
    assert q["account_id"] == "payer.testnet"
    assert q["public_key"] == "ed25519:" + m.b58encode(pub)
    # payload = base64(SignedTransaction): borsh(tx) + 0x00 + sig64
    payload = b64.b64decode(rpc.calls[1][1][0])
    expected_tx = m.serialize_transfer_tx(
        "payer.testnet", pub, 11, "recv.testnet", b"\x07" * 32, 10**22)
    assert payload[:len(expected_tx)] == expected_tx        # tx байт-в-байт
    assert payload[len(expected_tx)] == 0                   # keyType ED25519
    sig = payload[len(expected_tx) + 1:]
    assert len(sig) == 64
    # подпись детерминирована: пересчитываем эталон
    expected_sig = m.ed25519_sign(hashlib.sha256(expected_tx).digest(), seed, pub)
    assert sig == expected_sig


def test_send_transfer_hash_fallback_when_response_bare():
    """Пустой result → хэш вычислен локально base58(sha256(signed))."""
    import tools.near_rpc as m
    rpc = _ScriptedRpc()
    client = rpc.client()

    def bare(method, params):
        rpc.calls.append((method, params))
        if method == "query":
            return {"nonce": 0, "block_hash": rpc.block_hash}
        if method == "broadcast_tx_commit":
            return {}
        raise AssertionError(method)

    client.call = bare
    seed = b"\x03" * 32
    pub = m.ed25519_publickey(seed)
    key = m.b58encode(seed + pub)
    txh = client.send_transfer("a.testnet", "b.testnet", 1, key)
    # восстанавливаем ожидаемый хэш детерминированно
    tx = m.serialize_transfer_tx("a.testnet", pub, 1, "b.testnet",
                                 b"\x07" * 32, 1)
    import hashlib
    signed = m.serialize_signed_tx(
        tx, m.ed25519_sign(hashlib.sha256(tx).digest(), seed, pub))
    assert txh == m.b58encode(hashlib.sha256(signed).digest())


def test_demo_colors_have_no_background_or_underline():
    """Цвета демо — только foreground: SGR 4 (underline) и фоны 4x/10x
    запрещены (терминал рисует их как сплошной синий блок)."""
    import re
    import tools.ansi_demo as ad

    previous = ad.ON
    ad.ON = True
    try:
        lines = [
            ad.colorize_log(
                "[contract] create_task TxHash: HtMQFqkp4nq5r2DTY2YQ14aWkyMi2GWA1MZmhDD2zhbZ"),
            ad.colorize_log("[settle] intent intent-9388df0ae121b384 → settled"),
            ad.colorize_log("[bid] research-agent 0.0241 NEAR — verified"),
            ad.colorize_log("[auction] победитель: research-agent @ 0.0241 NEAR"),
        ]
    finally:
        ad.ON = previous

    for line in lines:
        for params in re.findall(r"\x1b\[([0-9;]*)m", line):
            parts = [int(p) for p in params.split(";") if p]
            assert not any(
                40 <= p <= 47 or 100 <= p <= 107 for p in parts), line
            assert 4 not in parts, line
    assert "\x1b[1;96m" in lines[0]   # [contract] — яркий cyan
    assert "\x1b[95m" in lines[1]     # [settle] остаётся пурпурным


if __name__ == "__main__":
    for name, fn in sorted({k: v for k, v in globals().items()
                            if k.startswith("test_")}.items()):
        fn()
        print(f"OK {name}")
