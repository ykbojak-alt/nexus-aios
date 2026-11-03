# Submission assets — X post, idea field, API payload draft

Черновики для финальной заявки. Плейсхолдеры `[USER_INPUT_*]` заполняет User.
Формат проверен против `docs/SUBMISSION.md` и контракта `POST
https://nearhackathon.com/api/applications` (max body 12000 bytes).

## 1. X post (≤ 280 chars — 234 chars без ссылки)

```
We built NEXUS AiOS for @NEARProtocol #Hackathon! Our multi-agent system uses NEAR Intents to let AI agents hire and pay each other. Watch our demo to see how agents research and transact autonomously. [DEMO_LINK]
```

Заменить `[DEMO_LINK]` на ссылку на видео/репозиторий перед постом.

## 2. Поле `idea` (20–2000 chars — ~720 chars)

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
```

## 3. JSON-payload draft (НЕ отправлять без ревью User)

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
  "idea": "AI agents can already research, write code, and review work — but they cannot pay each other for verified results. NEXUS AiOS solves this with a multi-agent swarm on NEAR Intents. An Architect decomposes a user task and auctions stages to specialized workers (research agent, coder, auditor); the cheapest cryptographically verified bid wins. The Research Agent runs a live Plan-Act-Observe loop: it queries an LLM, searches via Tavily, and synthesizes answers with [Source: URL] citations. Once cite_check verifies every citation, the nexus-escrow contract on NEAR Testnet releases payment to the worker: create_task deposits the budget, submit_bid carries an ed25519-signed price verified on-chain, and verify_and_release pays the winner. LoopGuard enforces role-based command allowlists and logs every allow/deny decision. The demo prints real TxHashes for every contract step.",
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
