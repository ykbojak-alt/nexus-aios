# NEXUS AiOS — NEAR Hackathon Submission

Tracks: `swarms`, `security`, `agents`

## Problem

- Do: give a task to a single AI agent. See: it works alone, is not accountable, cannot pay for help.
- Do: read agent output. See: no verifiable payment trail, no policy guard for agent-owned wallets.

## Solution

- Do: run NEXUS AiOS. See: a swarm of role-specialized agents (Architect, Coder, Runner, Auditor) coordinate on one task.
- Do: make a request. See: Architect agent hires a worker agent and pays it through a NEAR Intent — every task has an on-chain-verifiable payment record.
- Do: inspect `LoopGuard` + `command_policy` logs. See: every agent action passes a policy gate before touching a wallet or shell.

## How to run

- Do: `git clone <repo> && cd near-hackathon-nexus`. See: repo with `tools/`, `docs/`, `policies/`.
- Do: `python tools/research_agent_demo.py "your question"`. See: research answer with `[Source: url]` citations.
- Do: `python tools/orchestrator_demo.py`. See: Architect hires Coder, pays via `NearIntentPayer` stub, prints intent receipt.

## Architecture

```
User task
  → Architect agent (plans, decomposes, hires workers)
  → Worker agents (C.O.D.E.X./research/runner) execute tasks
  → NearIntentPayer posts NEAR Intent on task success
  → LoopGuard + command_policy validate every command before execution
  → Verdict + payment receipts + audit log
```

## NEAR Integration

- Do: Architect agent emits a `create_intent` call naming the worker, task hash, and reward. See: NEAR Intent settles payment to the worker agent's account.
- Do: a worker completes a verified task. See: `NearIntentPayer.pay(worker_id, task_id, amount)` produces an intent receipt `{intent_id, solver, status}`.
- Do: inspect `docs/near_architecture.md`. See: full intent lifecycle and LoopGuard policy mapping.

## Security (LoopGuard)

- Do: read `policies/command_policy.json`. See: allowlist of executable commands per agent role.
- Do: an agent tries a denied command. See: LoopGuard blocks execution and logs `denied` verdict — the wallet never signs a non-verified action.
