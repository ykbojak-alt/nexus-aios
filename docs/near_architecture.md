# NEXUS AiOS × NEAR — Architecture Spec (hackathon)

Official: NEAR HACKATHON 1–14.11.2026 (see HACKATHON.md). Submission API one-shot; repo public by deadline.

Track hints (official): swarms → *multi-agent systems, NEAR Intents, sub-accounts*; security → *agent security, function call keys, TEE attestation*; agents → *agent commerce, NEAR Intents, chain signatures*.

## Track: swarms

NEXUS AiOS is already a working multi-agent swarm:

| Role | Node | Function |
|---|---|---|
| Architect | Copilot/A.L.I.C.E. | decomposes user task, hires workers, verifies results |
| Coder | C.O.D.E.X. | writes/patches code |
| Runner | M.E.C.H. | executes benches/commands |
| Auditor | N.E.O. | collects evidence, triages failures |

Swarm mechanics: tasks are briefs; each worker returns a report; the Architect aggregates verdicts. For the hackathon we map **task → NEAR Intent**: when a worker delivers a verified result, the Architect settles a payment via the NEAR Intents contract — agent-to-agent market where work is provably paid.

## Track: agents

Research Agent MVP (`tools/research_agent_demo.py`): Tavily search → context filter → Nebius synthesis → cite_check. Agent's claims carry `[Source: url]`; unverified claims are flagged. On task completion, `NearIntentPayer` emits an intent receipt — the *agent* gets paid for verified work.

## Track: security — LoopGuard + command_policy

Problem: an agent that holds a wallet can sign anything a prompt tells it to. Guard: every executable action passes `LoopGuard.check(command, role)` before running.

- `policies/command_policy.json` — allowlist per role (runner may `python ...`, payer may `create_intent`, nobody may `rm -rf` or sign arbitrary txs).
- Denied action → `denied` verdict logged, execution skipped. The wallet key never leaves `os.environ`; the policy gate sits between the model output and the OS.
- Evidence: audit log per task `{command, role, verdict, ts}` — AI judges can replay policy decisions.

## NEAR Intents lifecycle

```
1. Architect creates task intent: {worker, task_hash, reward, deadline}
2. Worker accepts, executes, returns signed result
3. cite_check/verifier marks result verified
4. NearIntentPayer.pay() → intent settles (stub → real Intent API in prod)
5. Receipt {intent_id, solver, status} appended to audit log
```

Keys: `NEAR_ACCOUNT_ID`, `NEAR_PRIVATE_KEY` via env only. Wallet creation is manual (User's step, not code).
