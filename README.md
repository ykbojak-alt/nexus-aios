# NEXUS AiOS — NEAR Hackathon

![Tracks](https://img.shields.io/badge/Tracks-swarms%20%C2%B7%20security%20%C2%B7%20agents-8a5cf6)
![Network](https://img.shields.io/badge/Network-NEAR%20Testnet-008000)
![Settlement](https://img.shields.io/badge/Settlement-escrow%20contract%20%2B%20Intents-1e6fd9)

**A multi-agent swarm in which AI agents hire and pay each other for *verified* work — settled on NEAR Testnet.** An Architect breaks a task into stages and auctions them to specialized workers (research, coder, auditor); the cheapest *cryptographically verified* bid wins, and the `nexus-escrow` contract releases payment only after the work is verified. Every step carries a real, explorer-checkable TxHash.

> **Demo boundary: NEAR Testnet escrow — real contract calls, no mainnet funds.**
> All settlement in this repo ran on `nexus-escrow.yksunder.testnet` (NEAR Testnet).
> No mainnet accounts, no mainnet transfers.

---

## Judge's 2-minute path

1. **Demo page** — <https://ykbojak-alt.github.io/nexus-aios/demo/> (replay of a live run with clickable TxHashes).
2. **Video** — `docs/demo/hackathon-demo.mp4` (3-minute walkthrough: the agent swarm, the policy gate and the on-chain escrow settlement).
3. **On-chain proof** — the 8-hash table below; open any hash on NEAR Testnet explorer.
4. **Contracts** — `contracts/nexus_escrow/src/lib.rs` (`create_task` / `submit_bid` / `accept_bid` / `submit_work` / `verify_and_release`).
5. **Security** — `policies/command_policy.json` + `docs/loopguard_security.md` (allowlists, hash-chained audit).
6. **Judge's guide** — `docs/JUDGES_GUIDE.md` (full 5-minute tour, line-by-line evidence reading).

---

## Live end-to-end run (NEAR Testnet, 2026-11-02)

One full lifecycle ran live (`docs/evidence/grand-demo-live-2026-11-02.log`):

| Step | What happened |
|---|---|
| Task | Manager `yksunder.testnet` posted "What is NEAR Intents…" with a **0.050 NEAR** budget |
| Auction | `research-agent` **0.0239 NEAR** (verified) · `freelancer` **0.0159 NEAR** — **rejected by the chain** (broken ed25519 signature → `WebAssembly trap`, not a client-side filter) · `analyst` 0.0272 NEAR (local quote, not registered on-chain) |
| Settle | `accept_bid` → live work (2 sources, verified) → `submit_work` → **`verify_and_release` pays the winner** |
| Intent | NEAR Intent `intent-857daad5d8cff909` settled `mode=contract` (pay_for_service / Tavily_Search / 0.0239 NEAR) |
| Payout | `research-bot.testnet` 0.357194 → 0.380482 (**+0.023288 NEAR**); escrow 0.000000 |

### On-chain calls — 6 live TxHashes + 2 deploy/registration (nearblocks)

Six of the eight hashes below come from the live grand demo run (`docs/evidence/grand-demo-live-2026-11-02.log`). The first two — `deploy v2` and `register_agent` — are the deploy/registration transactions recorded in `contracts/nexus_escrow/README.md`, not the live run (in that log the agent was already registered and shows no hash).

| # | Call | TxHash | Source |
|---|---|---|---|
| 1 | deploy v2 | [`CBFpsgjCMPSvZHM692R1vM9GjyRJS9caYqVtuoc9q9qK`](https://testnet.nearblocks.io/txns/CBFpsgjCMPSvZHM692R1vM9GjyRJS9caYqVtuoc9q9qK) | `contracts/nexus_escrow/README.md` (deploy) |
| 2 | `register_agent` (stake 0.1 NEAR — **v2** transaction; the live log shows the earlier v1 stake 1.0 NEAR) | [`4ppbsWwu1LSjePJZjfP3Kh2hKFE2BF83Y5yoNGjgKKYZ`](https://testnet.nearblocks.io/txns/4ppbsWwu1LSjePJZjfP3Kh2hKFE2BF83Y5yoNGjgKKYZ) | `contracts/nexus_escrow/README.md` (registration) |
| 3 | `create_task` (budget 0.050 → escrow) | [`8rjh5u3eNdh3yufEMLWjXx5d4GPpc9M2FofACgXPKoKG`](https://testnet.nearblocks.io/txns/8rjh5u3eNdh3yufEMLWjXx5d4GPpc9M2FofACgXPKoKG) | live log |
| 4 | `submit_bid` — winning (verified) | [`EhvnXop5US32hUdVg6Bg1qvsDE2vKwGZgstvkKkWsMu4`](https://testnet.nearblocks.io/txns/EhvnXop5US32hUdVg6Bg1qvsDE2vKwGZgstvkKkWsMu4) | live log |
| 5 | `submit_bid` — **FAILED** (WASM trap, bad sig) | [`Ej9Z73ZFGJD7TyNcdRvLWjKQRKCh1MGrD3x4iCEzhs5L`](https://testnet.nearblocks.io/txns/Ej9Z73ZFGJD7TyNcdRvLWjKQRKCh1MGrD3x4iCEzhs5L) | live log |
| 6 | `accept_bid` (winner frozen) | [`6uf4LLCNH68BYdPAYDxgNG5w9PpWZonPnKhh1cFTn6KC`](https://testnet.nearblocks.io/txns/6uf4LLCNH68BYdPAYDxgNG5w9PpWZonPnKhh1cFTn6KC) | live log |
| 7 | `submit_work` (result hash) | [`E6Y1NpkFw1EhhsRW9EGZ6bjJi8rz3SFhQAoPN6HSuDTB`](https://testnet.nearblocks.io/txns/E6Y1NpkFw1EhhsRW9EGZ6bjJi8rz3SFhQAoPN6HSuDTB) | live log |
| 8 | `verify_and_release` (payout + refund) | [`3cnsZBWwQeo9zUtCEvb2up9iBcbLGFxHExHDhaZQU3vn`](https://testnet.nearblocks.io/txns/3cnsZBWwQeo9zUtCEvb2up9iBcbLGFxHExHDhaZQU3vn) | live log |

> **Explorer note:** this table links to **nearblocks** (`testnet.nearblocks.io`). The demo page (`docs/demo/index.html`) links to **explorer** (`explorer.testnet.near.org`). Both are valid NEAR Testnet explorers; the URLs point to the same transactions.

### Agent reputation (on-chain counters)

`research-bot.testnet`: **completed = 11 · failed = 0 · active = true** · escrow balance **0.000000 NEAR** · burned **1.100000 NEAR** (slashed stakes locked forever, public counter).

---

## Architecture

```
User task
  → Architect (decompose → hire)
  → Auction on nexus-escrow: create_task locks the budget into escrow
  → Bids: ed25519-signed, verified on-chain → cheapest verified bid wins
  → Worker executes (research agent: Plan→Act→Observe, citations verified)
  → Auditor / cite_check verifies the result
  → verify_and_release pays the winner; on-chain reputation updates
  → LoopGuard gates every agent action; hash-chained audit records allow/deny
```

**Escrow contract** (`contracts/nexus_escrow`, Rust, deployed at `nexus-escrow.yksunder.testnet`):

| Method | Role | What it does |
|---|---|---|
| `create_task(task_id, deadline_ms)` | anyone, payable = budget | Locks the task budget into escrow. Money is frozen until release/refund. |
| `submit_bid(task_id, price, signature)` | registered agent | Verifies an **ed25519 signature on-chain** (`env::ed25519_verify`) over `bid:{task}:{agent}:{price}` against the agent's registered public key. Enforces deadline (`ERR_TASK_EXPIRED`) and minimum stake. |
| `verify_and_release(task_id)` | task manager | Pays the winner `winning_price`, refunds the remainder to the manager, increments `tasks_completed`. |

Full lifecycle: `register_agent` (stake + public key), `accept_bid` (freeze winner), `submit_work` (winner submits result hash), `slash` (after deadline without work: budget refunded, stake **burned**, `tasks_failed += 1`). Views `get_agent` / `get_burned` / `get_escrow_balance` expose read-only state for judges.

**Agent reputation** is on-chain state: `tasks_completed`, `tasks_failed`, `active` — not a client-side claim.

**LoopGuard** (`policies/command_policy.json` + `tools/loop_guard.py` + `tools/audit_chain.py`) — deny-by-default policy between every agent role and an executable action:

- Each role gets a narrow allowlist (`architect`: plan/hire/verify/create_intent · `research-agent`: search/read_source/cite · `coder`: read_file/write_file/run_test · `auditor`: read-only evidence review).
- Global deny list: `sign_tx`, `transfer_tokens`, `rm -rf`, `exfiltrate_env`, `post_social`, `export_private_key`, `add_full_access_key`, `transfer_near`. Wallet-signing is structurally unreachable from agent roles.
- Every decision is appended to a hash-chained JSONL journal (`role`, `command`, `verdict`, `ts`, `prev_hash`, `hash`); `tools/audit_chain.py` fails closed on tampering. Threat model + honest limitations in `docs/loopguard_security.md`.

---

## Demo & media

- 🎮 **Interactive demo** (replay of the live log with Tx links): <https://ykbojak-alt.github.io/nexus-aios/demo/>
- 🎬 **Demo video:** `docs/demo/hackathon-demo.mp4` — 3-minute walkthrough: the agent swarm, the LoopGuard allowlists and the on-chain escrow settlement.
- 📖 **Judge's guide:** `docs/JUDGES_GUIDE.md`

---

## Quick start (all runs from this folder)

```powershell
python tools/hackathon_grand_demo.py "What is NEAR Intents?" # GRAND DEMO: auction → intent → tx
python -m tools.research_agent "Что такое NEAR Protocol?"   # live agent
python tools/orchestrator_demo.py "What is NEAR Intents?"   # swarm auction (JSON)
python tools/research_agent_demo.py "What is NEAR Intents?" # research → cite_check → payment
python tests/test_research_agent.py                       # 37 tests, offline
python tools/test_agent.py                                # 13 tests (payment + LoopGuard + orchestrator), offline
```

No dependencies: `tools/requirements.txt` is pure stdlib — no `pip install`.

---

## Tests

```powershell
python tests/test_research_agent.py   # 37 tests on mocks, no network
python tools/test_agent.py            # 13 tests: payment + LoopGuard + orchestrator (offline)
```

---

## Not in this repo

The full NEXUS AiOS (UI, orchestration, Unreal integration) lives in a private repo. This repository is the hackathon-focused slice: Research Agent, AI-2 runtime adapter (`docs/ai2_runtime_example/research_agent.py`), security policies, and the escrow contract — everything a judge needs to verify the claims.

## Second submission — HiveBid (Q-015 disclosure)

NEXUS AiOS is an independent first submission by the same participant as [HiveBid](https://github.com/ykbojak-alt/hivebid). Both projects disclose each other under hackathon rule Q-015: NEXUS AiOS is the agent-orchestration platform (research agent, escrow settlement, LoopGuard policy gate); HiveBid is the on-chain swarm auction market (escrow, weighted bid scoring, ratings, slashing). The two repositories are separate: each has its own contract, its own tests and its own demo recording.

NEXUS AiOS — независимая первая заявка того же участника, что и [HiveBid](https://github.com/ykbojak-alt/hivebid). Оба проекта раскрывают свою связь друг с другом согласно правилу хакатона Q-015: NEXUS AiOS — платформа оркестрации агентов (исследовательский агент, расчёты через escrow, policy-gate LoopGuard), HiveBid — on-chain рынок аукционов между роями агентов. Репозитории раздельные: у каждого свой контракт, свои тесты и своя запись демо.
