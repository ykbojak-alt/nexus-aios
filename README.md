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

## Requirements

- Python **3.11+**, **stdlib only** — no pip installs for the demo path.
- Optional env for **live mode** (`research_agent_demo.py`): `TAVILY_API_KEY`, `NEBIUS_API_KEY`. Without them the swarm demo runs fully offline on stubs.

## Do X — See Y (swarm run)

- Do: `python tools/orchestrator_demo.py "What is NEAR Intents?"`.
- See: architect decomposes the task into 3 stages (`swarm-1-research`, `swarm-2-code`, `swarm-3-audit`).
- See: **3 workers bid on every stage** — `research-agent`, `coder`, `auditor` quote `{worker, sub_account, skill, task_id, price, checksum}`; architect verifies checksum/skill/sub-account and picks the **cheapest verified bid**. A forged bid (bad checksum) is rejected and listed in `rejected_bids`.
- See: winners settled **each by a separate `NearIntentPayer.pay` receipt** — `solver` fields are NEAR-style sub-accounts: `research-agent.architect.near` (1.39), `coder.architect.near` (2.48), `auditor.architect.near` (1.02), all `status: settled`.
- See: `audit_log` — 10 LoopGuard entries (`decompose`/`hire`/`verify`/`create_intent`) — replayable evidence of every gated action.

## Tracks mapping

| Track | Feature in this repo |
|---|---|
| swarms | `tools/orchestrator_demo.py` — 3-worker bid auction, cheapest verified bid wins, per-task settlement to `*.architect.near` sub-accounts |
| security | `tools/loop_guard.py` + `policies/command_policy.json` — role allowlist (architect/research-agent/coder/runner/auditor), hard deny (`transfer_tokens`, `sign_tx`, `exfiltrate_env`), every action logged pre-execution |
| agents | `tools/research_agent_demo.py` + `tools/test_agent.py` — research worker with `[Source: url]` citations and unverified-claim flagging; wallet-free `NearIntentPayer` stub keeps the NEAR Intent receipt contract |

