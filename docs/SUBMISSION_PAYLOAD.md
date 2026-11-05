# Submission assets — X post, idea field, API payload draft

## ✅ ОТПРАВЛЕНО 2026-11-03

- `POST /api/applications` → **HTTP 201** (saved), один send без ретраев.
- `requestId`: `f206fa10-94e3-4537-a8e6-912a712697a9` (UUID v4, сгенерирован в момент send; если понадобится ретрай — только с ним).
- Верификация: `GET /api/projects` → total 80→**81**, проект виден в публичном каталоге с треками `["swarms","security","agents"]`.
- `consent: true` — одобрено User в send-команде.
- Тело: `D:\Temp\submit-payload.json` (1456 байт, idea 1074 chars).

Черновики для финальной заявки. Плейсхолдеры `[USER_INPUT_*]` заполняет User.
Формат проверен против `docs/SUBMISSION.md` и контракта `POST
https://nearhackathon.com/api/applications` (max body 12000 bytes).

## 1. X post — ✅ ОПУБЛИКОВАН вручную User (2026-11-03)

Published: https://x.com/yksunder/status/2117547178726228200

```
We built NEXUS AiOS for @NEARProtocol #Hackathon! Our multi-agent system uses NEAR Intents to let AI agents hire and pay each other. Watch the live demo: https://ykbojak-alt.github.io/nexus-aios/demo/
```

RAE-автопост отложен: X API отвечает 402 (квота/тариф) и 401 (user-креды). Пост ушёл руками — зачёт есть, демону повторять не нужно.

## 2. Поле `idea` (20–2000 chars — ~1100 chars)

```
AI agents can already research, write code, and review work — but they
cannot pay each other for verified results. NEXUS AiOS solves this with a
multi-agent swarm on NEAR Intents. An Architect decomposes a user task and
auctions stages to specialized workers (research agent, coder, auditor);
the cheapest cryptographically verified bid wins. The Research Agent runs
a live Plan-Act-Observe loop: it queries an LLM, searches via Tavily, and
synthesizes answers with [Source: URL] citations. Once cite_check verifies
every citation, the nexus-escrow contract on NEAR Testnet releases payment
to the worker: create_task deposits the budget, submit_bid carries an
ed25519-signed price verified on-chain, and verify_and_release pays the
winner. LoopGuard enforces role-based command allowlists and logs every
allow/deny decision. The demo prints real TxHashes for every contract step.
The Router/Worker split matches the "super-agent" architecture DeepSeek
published days before this hackathon; we add the layer the paper leaves
open — agents that settle verified work on-chain.
```

## 3. JSON-payload draft (НЕ отправлять без ревью User)

> ⚠ **Not the sent payload.** The `requestId` below (`4b3c161e-…`) is a **stale draft** — the actual submission used `f206fa10-94e3-4537-a8e6-912a712697a9` (see `docs/SUBMISSION.md`). This block is kept for reference only.

```json
{
  "requestId": "4b3c161e-f294-41c0-aa7d-6accf99bff2d",
  "projectName": "NEXUS AiOS",
  "leadName": "YKSunder",
  "email": "yk.bojak@gmail.com",
  "size": "1",
  "tracks": ["swarms", "security", "agents"],
  "nearAddress": "yk.sunder.near",
  "projectUrl": "https://github.com/ykbojak-alt/nexus-aios",
  "demoUrl": "https://ykbojak-alt.github.io/nexus-aios/demo/",
  "idea": "AI agents can already research, write code, and review work — but they cannot pay each other for verified results. NEXUS AiOS solves this with a multi-agent swarm on NEAR Intents. An Architect decomposes a user task and auctions stages to specialized workers (research agent, coder, auditor); the cheapest cryptographically verified bid wins. The Research Agent runs a live Plan-Act-Observe loop: it queries an LLM, searches via Tavily, and synthesizes answers with [Source: URL] citations. Once cite_check verifies every citation, the nexus-escrow contract on NEAR Testnet releases payment to the worker: create_task deposits the budget, submit_bid carries an ed25519-signed price verified on-chain, and verify_and_release pays the winner. LoopGuard enforces role-based command allowlists and logs every allow/deny decision. The demo prints real TxHashes for every contract step. The Router/Worker split matches the \"super-agent\" architecture DeepSeek published days before this hackathon; we add the layer the paper leaves open — agents that settle verified work on-chain.",
  "consent": true
}
```

### Перед отправкой

- `requestId`: сгенерировать реальный UUID один раз; ретраи — с тем же.
- `consent`: `true` только после явного согласия User на контакт-уведомление.
- `email`, `projectUrl`, `demoUrl` — заполнить реальными значениями.
- ⚠ В `docs/SUBMISSION.md` черновик писал `projectName: "NESUS AiOS"` —
  здесь исправлено на `NEXUS AiOS`; подтвердить финальное написание.
- Ответы API: 201 saved; 400 — проверить `fields`; 403 — не ретраить;
  409 — имя/email заняты (не подменять); 413 — сократить idea;
  503 — минута паузы, ретрай с тем же requestId.
