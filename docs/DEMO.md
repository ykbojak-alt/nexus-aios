# NEXUS AiOS — demo copy and 3-minute walkthrough

## X post — Russian

🤖 Представляем NEXUS AiOS для **NEAR HACKATHON**: swarm специализированных AI-агентов превращает задачу в проверяемый рабочий процесс. Architect координирует, Research Agent ищет источники, Auditor проверяет результат, а NEAR Intents показывают модель оплаты за подтверждённую работу. LoopGuard применяет role-based allowlists и блокирует опасные действия вроде `sign_tx`, оставляя след решения в audit log.

В демо подтверждённая работа оплачивается эскроу-контрактом `nexus-escrow.yksunder.testnet` на NEAR Testnet: `create_task` → `submit_bid` с ed25519-подписью → `verify_and_release`. Каждый шаг печатает реальный TxHash; рядом — policy gate и воспроизводимый журнал решений.

Демо: https://ykbojak-alt.github.io/nexus-aios/demo/
#NEARHACKATHON #NEAR #AI

## X post — English

🤖 Meet NEXUS AiOS at **NEAR HACKATHON**: a swarm of specialized AI agents turns a task into a reviewable workflow. The Architect coordinates, the Research Agent gathers sources, and the Auditor checks the result. NEAR Intents illustrate payment for verified work, while LoopGuard applies role-based allowlists, blocks dangerous actions such as `sign_tx`, and records each policy decision for review.

The demo settles verified work on NEAR Testnet through the `nexus-escrow` smart contract: the manager deposits a budget, agents submit ed25519-signed bids that the contract verifies, and `verify_and_release` pays the winner. Every step prints a real TxHash, alongside the policy gate and replayable decision log.

Demo: https://ykbojak-alt.github.io/nexus-aios/demo/
#NEARHACKATHON #NEAR #AI

## Three-minute video script

| Time | Scene / on-screen | Narration |
|---|---|---|
| 0:00–0:15 | Title card: “NEXUS AiOS — NEAR HACKATHON / Agent swarm + policy guard” | “NEXUS AiOS explores how specialized agents can collaborate on a task while keeping actions reviewable.” |
| 0:15–0:40 | Show repo and architecture flow: Architect → workers → verification → intent receipt → LoopGuard audit | “The Architect decomposes work. Research and coding roles have separate capabilities; the Auditor collects evidence. A verified result can produce an intent receipt in the demo flow.” |
| 0:40–1:05 | Open `policies/command_policy.json`; highlight each role’s `allow` list and global `deny` entries | “Every role gets a narrow allowlist. Wallet-sensitive operations such as signing, token transfers, and access-key changes are denied. Unknown commands fail closed because they are not on a role’s allowlist.” |
| 1:05–1:35 | Terminal shows the local test output, especially `PASS test_loopguard_denies_sign`; show the denied event fields `command`, `role`, `verdict`, `ts`, `prev_hash`, `hash` | “A hostile instruction can ask the coder to sign a transaction, but that prompt does not grant permission. LoopGuard records the denied decision and `require` raises before the caller proceeds.” |
| 1:35–2:00 | Show `tools/orchestrator_demo.py` output or a prepared, redacted run: result, audit log, payment receipt | “The judge can inspect the role, requested command, verdict, timestamp, and receipt together. LoopGuard persists decisions in a hash-chained JSONL journal; a trusted external checkpoint is needed to detect a full rewrite or tail truncation.” |
| 2:00–2:25 | Diagram: NEAR FullAccess key vs restricted FunctionCall key: receiver, method names, gas allowance | “The capability model is similar to a NEAR FunctionCall access key: constrain the receiver, methods, and gas allowance. Keep FullAccess credentials away from agent runtime. The chain key and LoopGuard are separate layers.” |
| 2:25–2:45 | On-screen callout: “Demo boundary: NEAR Testnet escrow — real contract calls, no mainnet funds” | “Settlement is real on NEAR Testnet: create_task funds escrow, the contract verifies each signed bid, and verify_and_release pays the winner. The security design keeps FullAccess credentials out of the agent runtime; LoopGuard and the contract are separate layers.” |
| 2:45–3:00 | Closing card with the published demo URL and hackathon title | “NEXUS AiOS: collaborative agents, explicit capabilities, and evidence a judge can inspect. Explore the demo at https://ykbojak-alt.github.io/nexus-aios/demo/.” |

## Capture notes

- Use https://ykbojak-alt.github.io/nexus-aios/demo/ as the published demo URL.
- Use a prepared, redacted orchestration output; do not show environment variables, private keys, or API tokens.
- Whenever showing the receipt, keep the real Testnet TxHashes visible and state that settlement ran on NEAR Testnet, not mainnet.
- The unit-test output is a local verification result; the on-chain evidence is the Testnet TxHashes in the demo log.

