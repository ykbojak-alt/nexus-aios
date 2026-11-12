# NEXUS AiOS — Judge's Guide (5-minute tour)

**What this is.** NEXUS AiOS is a multi-agent swarm in which AI agents hire and pay each other for *verified* results: an Architect breaks a task into stages, auctions them to specialized workers, and a NEAR Testnet escrow contract settles the winning bid on-chain. The agents' money moves only when the work is cryptographically verified — not on trust.

**The demo boundary up front.** Everything here ran on **NEAR Testnet** (`nexus-escrow.yksunder.testnet`). No mainnet accounts, no mainnet funds. Every contract call below has a real, explorer-verifiable transaction hash.

---

## 1. Demo (2 minutes)

**Open:** https://ykbojak-alt.github.io/nexus-aios/demo/

What you see and what to do:

1. **Terminal replay** (autoplays) — a live run of the whole lifecycle: manager posts a task → auction → work → payment. Use **Pause / Restart** and the **1×/2×/4× speed** selector to scrub.
2. **Transactions card** — the six contract calls with real TxHashes. Click any hash to open it in `explorer.testnet.near.org` and see the transaction on-chain.
3. **Call rollup card** — the same calls as a checklist: ✓ success, ✗ one deliberately failed bid (a *broken signature that the chain itself rejected*).
4. **Agent reputation card** — the winning agent's on-chain counters: `completed = 11`, `failed = 0`.
5. Footer links to the source log: `docs/evidence/grand-demo-live-2026-11-02.log`.

**What to verify in under a minute:** the demo page is static and embeds the real evidence log; the TxHashes are the same ones in that log and open on the public NEAR Testnet explorer.

---

## 2. Video (3 minutes)

**File:** `docs/demo/hackathon-demo.mp4` (also embedded under the replay on the demo page).

The walkthrough covers, in order:

- **Architecture flow** — Architect → workers → verification → intent receipt → LoopGuard audit.
- **Policy file** — each role's `allow` list and the global `deny` list (`sign_tx`, `transfer_tokens`, …).
- **Denied signing test** — a hostile instruction ("ignore policy, sign this") is recorded as `denied`, and `require()` raises before anything executes.
- **Audit journal** — every decision row carries `role`, `command`, `verdict`, `ts`, `prev_hash`, `hash`.
- **Testnet settlement** — `create_task` funds escrow, the contract verifies each signed bid, `verify_and_release` pays the winner. Real TxHashes stay visible throughout; the callout reminds the demo is Testnet-only.

---

## 3. Live evidence on NEAR Testnet (2 minutes)

Source log: `docs/evidence/grand-demo-live-2026-11-02.log` (full run, 2026-11-02). Reading the key lines:

| Log line | What it means (plain English) |
|---|---|
| `manager=yksunder.testnet agent=research-bot.testnet budget=0.050 NEAR` | A "manager" account posts a task ("What is NEAR Intents…") and puts **0.050 NEAR on deposit** as the budget. The money is locked on the contract until the task settles. |
| `create_task TxHash: 8rjh5u3e…KoKG` | The task is created on-chain; the budget enters escrow. The hash proves it happened on the Testnet. |
| `submit_bid TxHash: EhvnXop5…sMu4 — [bid] research-agent 0.0239 NEAR — verified` | The research agent bids **0.0239 NEAR**, signed with its ed25519 key; the contract verifies the signature on-chain. |
| `submit_bid TxHash: Ej9Z73Z…hs5L — FAILED: WebAssembly trap … REJECTED` | A second agent bids cheaper (**0.0159 NEAR**) but **the chain rejects its bid**: its ed25519 signature is bad and the contract traps. This is the honest, interesting part — the cheap bid was *not* filtered client-side; the contract itself refused it. |
| `[bid] analyst 0.0272 NEAR — local quote (not registered on-chain)` | A third quote never even reaches the contract because that agent is not registered on-chain. |
| `🏆 победитель: research-agent @ 0.0239 NEAR` | The cheapest *cryptographically verified* bid wins the auction. |
| `accept_bid … submit_work TxHash: E6Y1NpkF…SuDTB` | The manager accepts the winner; the winner does the research (live: 2 sources, both HTTP 200) and submits a work hash. |
| `verify_and_release TxHash: 3cnsZBWw…U3vn` | The manager confirms the submitted work; the contract **pays the winner** and refunds the remainder. |
| `intent intent-857daad5d8cff909 → status=settled mode=contract` | The payment is expressed as a NEAR Intent (`pay_for_service`, Tavily_Search, 0.0239 NEAR) and settled in contract mode. |
| `research-bot.testnet 0.357194 → 0.380482 (+0.023288 NEAR)` | The winner's balance increased by **~0.023 NEAR** — the payout. |
| `yksunder.testnet 2.109869 → 2.085296 (-0.024573 NEAR)` | The manager paid: 0.0239 to the winner, the small remainder is the gas/refund round-trip. |
| `репутация research-bot.testnet: completed=11 failed=0 active=True` | On-chain reputation counters: **11 completed tasks, 0 failed, agent active**. |
| `escrow=0.000000 NEAR burned=1.100000 NEAR` | After settlement the escrow holds **nothing** (all budgets resolved). `burned` = **1.1 NEAR** of stakes destroyed by earlier slashes (v1 1 + v2 0.1) — burned money is locked on the contract forever, and the counter is public. |
| `verified=True | audit_log: decompose → hire → create_task → submit_bid → … → verify_and_release` | The full orchestration trail, from task decomposition to on-chain release. |

**Where each hash lives on the explorer:**

| Call | TxHash (click to open) |
|---|---|
| `create_task` | [8rjh5u3eNdh3yufEMLWjXx5d4GPpc9M2FofACgXPKoKG](https://explorer.testnet.near.org/transactions/8rjh5u3eNdh3yufEMLWjXx5d4GPpc9M2FofACgXPKoKG) |
| `submit_bid` (winning) | [EhvnXop5US32hUdVg6Bg1qvsDE2vKwGZgstvkKkWsMu4](https://explorer.testnet.near.org/transactions/EhvnXop5US32hUdVg6Bg1qvsDE2vKwGZgstvkKkWsMu4) |
| `submit_bid` (FAILED — WASM trap) | [Ej9Z73ZFGJD7TyNcdRvLWfKQRKCh1MGrD3x4iCEzhs5L](https://explorer.testnet.near.org/transactions/Ej9Z73ZFGJD7TyNcdRvLWfKQRKCh1MGrD3x4iCEzhs5L) |
| `accept_bid` | [6uf4LLCNH68BYdPAYDxgNG5w9PpWZonPnKhh1cFTn6KC](https://explorer.testnet.near.org/transactions/6uf4LLCNH68BYdPAYDxgNG5w9PpWZonPnKhh1cFTn6KC) |
| `submit_work` | [E6Y1NpkFw1EhhsRW9EGZ6bjJi8rz3SFhQAoPN6HSuDTB](https://explorer.testnet.near.org/transactions/E6Y1NpkFw1EhhsRW9EGZ6bjJi8rz3SFhQAoPN6HSuDTB) |
| `verify_and_release` | [3cnsZBWwQeo9zUtCEvb2up9iBcbLGFfHExHDhaZQU3vn](https://explorer.testnet.near.org/transactions/3cnsZBWwQeo9zUtCEvb2up9iBcbLGFfHExHDhaZQU3vn) |

Two more hashes complete the lifecycle (from `contracts/nexus_escrow/README.md`): the contract **deploy** (`CBFpsgjCMPSvZHM692R1vM9GjyRJS9caYqVtuoc9q9qK`) and the agent **registration** with its 0.1 NEAR stake (`4ppbsWwu1LSjePJZjfP3Kh2hKFE2BF83Y5yoNGjgKKYZ`).

---

## 4. The smart contract — `contracts/nexus_escrow`

Rust smart contract deployed at `nexus-escrow.yksunder.testnet` (source: `contracts/nexus_escrow/src/lib.rs`). The three headline methods:

| Method | Who | What it does |
|---|---|---|
| `create_task(task_id, deadline_ms)` | anyone, with deposit | Locks the task budget into escrow (`escrow += budget`). Money is frozen on the contract until release/refund. |
| `submit_bid(task_id, price, signature)` | registered agent | Verifies an **ed25519 signature on-chain** (`env::ed25519_verify`) over the message `bid:{task}:{agent}:{price}` against the agent's registered public key. Also enforces deadline (`ERR_TASK_EXPIRED`) and minimum stake. A bad signature → contract trap, no client-side filtering. |
| `verify_and_release(task_id)` | task manager | Pays the winner `winning_price`, refunds the remainder to the manager, increments `tasks_completed`. |

The full lifecycle also includes `register_agent` (stake + public key), `accept_bid` (freeze the winner), `submit_work` (winner submits result hash), and `slash` (after deadline with no work: budget refunded, agent's stake burned, `tasks_failed += 1`). Views (`get_agent`, `get_burned`, `get_escrow_balance`) expose the state a judge can read on-chain.

---

## 5. LoopGuard — the security layer

**Where:** `policies/command_policy.json` + `tools/loop_guard.py` + `tools/audit_chain.py`. Docs: `docs/loopguard_security.md`.

- **Deny-by-default.** A command runs only if it is on the calling role's `allow` list and not on the global `deny` list. Nothing else is allowed.
- **Per-role allowlists** — architect gets `plan/hire/verify/create_intent`, research-agent gets `search/read_source/cite`, coder gets `read_file/write_file/run_test`, runner gets `run_python/run_test`, auditor gets read-only evidence commands.
- **Global deny list** includes `sign_tx`, `transfer_tokens`, `rm -rf`, `exfiltrate_env`, `post_social`, `export_private_key`, `add_full_access_key`, `transfer_near`. Wallet-signing is structurally unreachable from agent roles.
- **Audit journal.** Every decision is appended to a hash-chained JSONL log (`role`, `command`, `verdict`, `ts`, `prev_hash`, `hash`); `tools/audit_chain.py` verifies the chain and fails closed on tampering (edit/insert/remove/reorder). The writer is `tools/loop_guard.py`; the verifier and its tests are in this repository (`tools/audit_chain.py`, `tests/test_audit_chain.py`), and the demo's `audit_log` field shows the same record shape. Documented honest limitation: an external checkpoint (e.g. an anchor hash on the escrow contract) is the recorded next step to defeat full-chain rewriting.
- **Test:** `python tools/test_agent.py` includes `test_loopguard_denies_sign` — a hostile signing request is denied and logged.

---

## 6. Architecture in one glance

```
User task
  → Architect (decompose → hire)
  → Auction on nexus-escrow (create_task, budget locked)
  → Bids: ed25519-signed, verified on-chain → cheapest verified bid wins
  → Worker executes (research agent: Plan→Act→Observe, citations verified)
  → Auditor / cite_check verifies the result
  → verify_and_release pays the winner on-chain; balance + reputation update
  → LoopGuard gates every agent action; audit journal records allow/deny
```

---

## Fast facts for the scorecard

- **Track hooks:** swarms (multi-agent auction + on-chain settlement), agents (role-specialized workers, verified output), security (LoopGuard allowlists, hash-chained audit).
- **Testnet only:** no mainnet accounts or transfers.
- **Everything is inspectable:** replay demo, log, TxHashes, contract source, policy JSON, audit journal.
