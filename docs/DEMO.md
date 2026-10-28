# NEXUS AiOS — demo copy and 3-minute walkthrough

## X post — Russian

🤖 Представляем NEXUS AiOS для **NEAR HACKATHON**: swarm специализированных AI-агентов превращает задачу в проверяемый рабочий процесс. Architect координирует, Research Agent ищет источники, Auditor проверяет результат, а NEAR Intents показывают модель оплаты за подтверждённую работу. LoopGuard применяет role-based allowlists и блокирует опасные действия вроде `sign_tx`, оставляя след решения в audit log.

В демо intent receipt — локальная заглушка, без реальной транзакции или кошелька. Показываем архитектуру, policy gate и воспроизводимый журнал решений.

Демо: [DEMO_LINK]
#NEARHACKATHON #NEAR #AI

## X post — English

🤖 Meet NEXUS AiOS at **NEAR HACKATHON**: a swarm of specialized AI agents turns a task into a reviewable workflow. The Architect coordinates, the Research Agent gathers sources, and the Auditor checks the result. NEAR Intents illustrate payment for verified work, while LoopGuard applies role-based allowlists, blocks dangerous actions such as `sign_tx`, and records each policy decision for review.

The demo’s intent receipt is a local stub—there is no live wallet or on-chain transaction. We’re showing the architecture, policy gate, and replayable decision log.

Demo: [DEMO_LINK]
#NEARHACKATHON #NEAR #AI

## Three-minute video script

| Time | Scene / on-screen | Narration |
|---|---|---|
| 0:00–0:15 | Title card: “NEXUS AiOS — NEAR HACKATHON / Agent swarm + policy guard” | “NEXUS AiOS explores how specialized agents can collaborate on a task while keeping actions reviewable.” |
| 0:15–0:40 | Show repo and architecture flow: Architect → workers → verification → intent receipt → LoopGuard audit | “The Architect decomposes work. Research and coding roles have separate capabilities; the Auditor collects evidence. A verified result can produce an intent receipt in the demo flow.” |
| 0:40–1:05 | Open `policies/command_policy.json`; highlight each role’s `allow` list and global `deny` entries | “Every role gets a narrow allowlist. Wallet-sensitive operations such as signing, token transfers, and access-key changes are denied. Unknown commands fail closed because they are not on a role’s allowlist.” |
| 1:05–1:35 | Terminal shows the local test output, especially `PASS test_loopguard_denies_sign`; show the denied event fields `command`, `role`, `verdict`, `ts` | “A hostile instruction can ask the coder to sign a transaction, but that prompt does not grant permission. LoopGuard records the denied decision and `require` raises before the caller proceeds.” |
| 1:35–2:00 | Show `tools/orchestrator_demo.py` output or a prepared, redacted run: result, audit log, payment receipt | “The judge can inspect the role, requested command, verdict, timestamp, and receipt together. The current audit log is an in-memory demo artifact; production would persist it with a policy hash and tamper evidence.” |
| 2:00–2:25 | Diagram: NEAR FullAccess key vs restricted FunctionCall key: receiver, method names, gas allowance | “The capability model is similar to a NEAR FunctionCall access key: constrain the receiver, methods, and gas allowance. Keep FullAccess credentials away from agent runtime. The chain key and LoopGuard are separate layers.” |
| 2:25–2:45 | On-screen callout: “Demo boundary: intent payer is a stub; no wallet/network/on-chain settlement” | “To be precise, this repository’s payer is a stub. The receipt shown here is not a live NEAR transaction. The security design describes the boundary required before connecting a real wallet.” |
| 2:45–3:00 | Closing card with repository/demo URL placeholder and hackathon title | “NEXUS AiOS: collaborative agents, explicit capabilities, and evidence a judge can inspect. Explore the demo at [DEMO_LINK].” |

## Capture notes

- Replace `[DEMO_LINK]` with the published demo URL before posting.
- Use a prepared, redacted orchestration output; do not show environment variables, private keys, or API tokens.
- Keep the “local stub / no live transaction” disclosure visible or spoken whenever showing the intent receipt.
- The test output is a local verification result, not proof of production wallet integration.
