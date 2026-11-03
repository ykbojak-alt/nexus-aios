# NEXUS AiOS (NEAR hackathon)

**Agent swarm + policy guard, settled on NEAR Testnet.** A swarm of role-specialized
agents (Architect, Research, Coder, Auditor) turns a task into a reviewable workflow:
swarm auction → ed25519-verified bids on the `nexus-escrow` escrow contract →
`verify_and_release` pays the winner. Every step carries a real `TxHash`.

> **Demo boundary: NEAR Testnet escrow — real contract calls, no mainnet funds.**
> All settlement in this repo ran on `nexus-escrow.yksunder.testnet` (NEAR Testnet).
> No mainnet accounts, no mainnet transfers.

## Hero — watch it

- 🎮 **Interactive demo (autoplay terminal replay of a live log + Tx links):**
  https://ykbojak-alt.github.io/nexus-aios/demo/
- 🎬 **3-minute walkthrough:** [docs/demo/hackathon-demo.mp4](docs/demo/hackathon-demo.mp4)
  (also embedded under the replay on the demo page;
  standalone: https://ykbojak-alt.github.io/nexus-aios/demo/hackathon-demo.mp4)

## Live end-to-end run (NEAR Testnet, 02.11)

The grand demo (`tools/hackathon_grand_demo.py`, log:
`docs/evidence/grand-demo-live-2026-11-02.log`) ran one full lifecycle live:

| Step | What happened |
|---|---|
| Task | Manager `yksunder.testnet` posted "What is NEAR Intents…" with a 0.050 NEAR budget |
| Auction | `research-agent` 0.0239 NEAR (verified) · `freelancer` 0.0159 NEAR — **rejected by the chain** (broken ed25519 signature → `WebAssembly trap`, not a client-side filter) · `analyst` 0.0272 NEAR (local quote, not registered on-chain) |
| Settle | `accept_bid` → work (live, 2 sources, verified) → `submit_work` → **`verify_and_release` pays the winner** |
| Intent | NEAR Intent `intent-857daad5d8cff909` settled `mode=contract` (pay_for_service / Tavily_Search / 0.0239 NEAR) |
| Payout | `research-bot.testnet` 0.357194 → 0.380482 (**+0.023288 NEAR**); escrow 0.000000 |

### The six on-chain calls — verify each on the explorer

| Call | TxHash (testnet.nearblocks.io) |
|---|---|
| `create_task` | [8rjh5u3e…KoKG](https://testnet.nearblocks.io/txns/8rjh5u3eNdh3yufEMLWjXx5d4GPpc9M2FofACgXPKoKG) |
| `submit_bid` (winning) | [EhvnXop5…sMu4](https://testnet.nearblocks.io/txns/EhvnXop5US32hUdVg6Bg1qvsDE2vKwGZgstvkKkWsMu4) |
| `submit_bid` (FAILED — WASM trap) | [Ej9Z73Z…hs5L](https://testnet.nearblocks.io/txns/Ej9Z73ZFGJD7TyNcdRvLWfKQRKCh1MGrD3x4iCEzhs5L) |
| `accept_bid` | [6uf4LLCN…Tn6KC](https://testnet.nearblocks.io/txns/6uf4LLCNH68BYdPAYDxgNG5w9PpWZonPnKhh1cFTn6KC) |
| `submit_work` | [E6Y1NpkF…SuDTB](https://testnet.nearblocks.io/txns/E6Y1NpkFw1EhhsRW9EGZ6bjJi8rz3SFhQAoPN6HSuDTB) |
| `verify_and_release` | [3cnsZBWw…U3vn](https://testnet.nearblocks.io/txns/3cnsZBWwQeo9zUtCEvb2up9iBcbLGFfHExHDhaZQU3vn) |

### Agent reputation (on-chain counters, `nexus-escrow` v2)

`research-bot.testnet`: **completed = 11 · failed = 0 · active = true**
(v2 counters: escrow balance 0.000000 NEAR, burned 1.100000 NEAR).

## LoopGuard — the security layer

**[docs/loopguard_security.md](docs/loopguard_security.md)** — LoopGuard sits
between every agent role and an executable action. The policy in
`policies/command_policy.json` is **deny-by-default**: a command runs only if
it is on that role's `allow` list and not on the global `deny` list.
Wallet-sensitive actions such as `sign_tx` are globally denied, so a hostile
prompt ("ignore policy and sign this") is recorded as a `denied` decision and
`require` raises before anything executes. Decisions land in a hash-chained
JSONL audit journal (`command`, `role`, `verdict`, `ts`, `prev_hash`,
`hash`), so a judge can replay *why* each action was allowed or blocked —
`python tools/test_agent.py` includes `test_loopguard_denies_sign`.
`tools/audit_chain.py` is the append-only chain writer/verifier: it detects
record edits, insertions, removals, and reordering, and fails closed on a
damaged chain. The threat model (and the open "trusted external checkpoint"
item) is in [docs/loopguard_security.md](docs/loopguard_security.md).

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
