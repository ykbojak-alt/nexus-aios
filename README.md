# NEXUS Research Agent (NEAR hackathon)

## Relationship to NEXUS AiOS

This repository is a hackathon-focused slice. The full NEXUS AiOS (UI,
orchestration, Unreal integration) lives in our private repo. Here you can
see the Research Agent, the AI-2 runtime adapter, and the security policies.

## Что здесь

Живой agent loop: вопрос → Plan (LLM) → Act (Tavily search) → Observe
(оценка LLM) → повтор до 3 итераций → синтез со ссылками `[Source: URL]`.

## Быстрый старт (всё запускается из этой папки)

```powershell
python tools/hackathon_grand_demo.py "What is NEAR Intents?" # GRAND DEMO: аукцион → интент → tx
python -m tools.research_agent "Что такое NEAR Protocol?"   # живой агент
python tools/orchestrator_demo.py "What is NEAR Intents?"   # swarm-аукцион (JSON)
python tools/research_agent_demo.py "What is NEAR Intents?" # research → cite_check → payment
python tests/test_research_agent.py                       # 37 тестов, офлайн
python tools/test_agent.py                                # 13 тестов, офлайн
```

Зависимостей нет: `tools/requirements.txt` — чистый stdlib, `pip install`
не нужен.

## Ключи и модели — две конфигурации

**`.env.example` оставляет LLM-поля пустыми.** Без заполненных `LLM_*` и
AI-2-алиасов standalone-клиент использует модель `gpt-4o-mini` и endpoint
`https://api.openai.com/v1`. Cohere через OpenRouter и другие OpenAI-
совместимые провайдеры подключаются явной настройкой `LLM_MODEL`,
`LLM_BASE_URL` и `LLM_API_KEY`. AI-2 runtime с `mercury-3` на
`api.inceptionlabs.ai` — отдельная конфигурация.

Резолв `research_agent_clients.env()`: **прямое имя** (`LLM_*`,
`TAVILY_API_KEY`, `NEAR_*`) из `.env`/`os.environ` → **алиасы ai2**
(`OPENAI_*`, `AI2_MODEL_*`) из файла настроек `AI2_ENV_FILE` → `os.environ`.
Прямое имя бьёт алиас — поэтому `.env` с `LLM_*` даёт Cohere-контур,
а когда `LLM_*` нет, standalone наследует настройки рантайма
(mercury-3/inceptionlabs, ключ — из пула `AI2_PROVIDER_KEYS_JSON` по URL
endpoint'а, как `resolve_openai_env`).

| имя агента | standalone (.env) | фолбэк — настройки ai2 |
|---|---|---|
| `LLM_MODEL` | пусто в примере; без алиаса — `gpt-4o-mini` | `AI2_MODEL_EXECUTOR` → `mercury-3` |
| `LLM_BASE_URL` | пусто в примере; без алиаса — `https://api.openai.com/v1` | `OPENAI_BASE_URL` → `api.inceptionlabs.ai` |
| `LLM_API_KEY` | `LLM_API_KEY` напрямую | пул `AI2_PROVIDER_KEYS_JSON` по URL → `OPENAI_API_KEY`/`INCEPTION_API_KEY` |
| `TAVILY_API_KEY` | `TAVILY_API_KEY` | `AI2_TAVILY_API_KEY` (user env) |
| `NEAR_*` | `.env` (см. `.env.example`) | — |

## Адаптер рантайма AI-2

`docs/ai2_runtime_example/research_agent.py` — reference implementation
from the main NEXUS AiOS runtime: тот же цикл внутри рантайма, где вызовы
идут через `call_model` (бюджет, метрики, траектория), а поиск виден как
`tool_call[tavily_search]` + `research_iteration` (plan/search/observe/
answer). Сам рантайм живёт в приватном репо NEXUS AiOS — здесь адаптер
для судей; рабочая standalone-копия цикла — `tools/research_agent.py`.

## Architecture note

Two deliberate model stacks: the AI-2 runtime runs `mercury-3` via
`api.inceptionlabs.ai` (`AI2_MODEL_EXECUTOR`/`OPENAI_BASE_URL` in the
settings file, key from the `AI2_PROVIDER_KEYS_JSON` pool by endpoint
URL), while this standalone repo runs Cohere via OpenRouter from `.env`
(`LLM_MODEL`/`LLM_BASE_URL`/`LLM_API_KEY`). Both OpenAI-compatible;
secrets live in `.env`/user env and never reach git — `.env*` is
gitignored except `.env.example`.

## Демо-контур (swarm + intents)

- `tools/hackathon_grand_demo.py` — **единый прогон**: задача → аукцион →
  работа победителя → NEAR Intent → расчёт → ответ. Печатает торг вживую.
- `tools/orchestrator_demo.py` — аукцион одной задачи: менеджер объявляет
  задачу с бюджетом; `research-agent` (research-bot.testnet, на 12% дешевле
  аналитика) и `analyst` (стаб) торгуются, bid `freelancer` с битой
  ed25519-подписью отклоняется контрактом; cheapest-verified bid побеждает;
  расчёт — `verify_and_release` на `nexus-escrow.yksunder.testnet`.
- `tools/near_intent_payer.py` — расчёт через эскроу-контракт: чек несёт
  JSON-намерение `{"action": "pay_for_service", "service": "Tavily_Search",
  "amount": ..., "currency": "NEAR", "intent_id": ..., "deadline_ms": ...}`
  и реальные TxHash шагов `create_task`/`submit_bid`/`verify_and_release`.
- `tools/research_agent_demo.py` — пайплайн verified-answer → payment;
  `run_research_loop()` — мост живого агента в cite_check → NearIntentPayer.
- `tools/loop_guard.py` + `policies/command_policy.json` — policy gate,
  audit log решений (`decompose → hire → verify → create_intent`).

## NEAR Testnet Integration

`tools/near_rpc.py` — JSON-RPC клиент к **реальному** NEAR Testnet
(`https://test.rpc.fastnear.com`, переопределяется `NEAR_RPC_URL`), чистый
stdlib. Fallback/direct-transfer удалён: `NearIntentPayer.pay()` имеет два
режима, и в contract-режиме деньги двигает только контракт.

| режим | условие | что происходит |
|---|---|---|
| `stub` | нет `NEAR_ACCOUNT_ID`/`NEAR_ESCROW_CONTRACT` | локальный чек, без сети (тесты, офлайн) |
| `contract` | `NEAR_ACCOUNT_ID` + `NEAR_ESCROW_CONTRACT` | полный жизненный цикл `nexus-escrow.yksunder.testnet`: `create_task` → `submit_bid` → `accept_bid` → `submit_work` → `verify_and_release`; в чеке реальные `tx_hash` каждого шага + explorer |

Контракт v2: репутация агента (`tasks_completed`/`tasks_failed`/`active`),
проверка дедлайна (`ERR_TASK_EXPIRED`), счётчики `get_escrow_balance` /
`get_burned`. Ставки проверяются на цепочке: `submit_bid` несёт подпись
`ed25519_verify` от аккаунта агента; битый ключ отклоняется сетью
(`WebAssembly trap`), а не клиентом.

Ключи — только из `.env`/env (см. `.env.example`), в коде не хранятся и в
лог не печатаются. Подпись транзакции — **встроенная в `tools/near_rpc.py`**
(pure-Python ed25519 по RFC 8032 + Borsh-сериализация +
`broadcast_tx_commit`): `pip install` не нужен. near-api-py/py-near
сознательно не используются — их цепочки зависимостей (`ed25519`,
`versioneer`, `borsh-construct`) не собираются на Python 3.15. Ошибка
контракта → `ContractCallError` с сырым текстом, чек `status: "error"`;
«settled» понарошку не рисуется.

Цепочка для видео: verified answer → `verify_and_release` → реальные
`TxHash` в выводе `hackathon_grand_demo.py` + чек в JSON с
`explorer`-ссылкой на `testnet.nearblocks.io`.

## Тесты

```powershell
python tests/test_research_agent.py   # 37 тестов на моках, без сети
python tools/test_agent.py            # 13 тестов: оплата + LoopGuard + оркестратор (офлайн)
```
