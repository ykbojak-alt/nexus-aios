# HIVEBID SUBMISSION EVIDENCE
Date: 2026-11-08. Mode: documentation sync only, no commits, no build.

## 1. Commit-backed provenance
- `c365475` (A.C.E.) — idempotent deployment fix (`--redeploy`) for `deploy.sh`.
- `4983075` — add `hivebid_market` contract + challenge evidence.
- `7932896` — novelty/maturity documentation blocks (40/40/20 weights, 100 tests, audit chain).
- `dfdcd54` — demo video capture (99 s, recorded testnet auction) + page/README links.

## 2. Contract (on-chain)
- Contract account: `hivebid-yksunder.testnet`
- Manager / agent accounts: `hivebid-manager-yks.testnet`, `hivebid-agent-yks.testnet`
- Sub-swarms: `roy-b.hivebid-manager-yks.testnet`, `roy-c.hivebid-manager-yks.testnet`
- Deployed code hash (RPC `view_account`): `DCcTQUfWB7jW7N1DCfZpYjDp4K73NJ215vu1QcQHPmUf`
- Artifact: `hivebid_market.wasm`, 468,209 B, md5 `D3C8D79D`, sha256 `B545B8ADB25B5B92B2233D32AE5E6C4F026852C8CEAE7B5B074331AFB8EFB284`
- Tests/build evidence: `99/99` baseline documents, current build harvest 100/100 at commit `74461947` (W14). No new source changes were made during report generation.
- View entrypoints verified: `get_agent`, `get_lot`, `get_bid_scores`, `get_escrow`.
- Live states used in evidence:
  - agent ROY-A: 4N stake, rating 440000, 1 completed, 1 failed.
  - agent ROY-B: 1N stake, rating 500000, no tasks.
  - agent ROY-C: 1N stake, rating 635500, 3 completed, 0 failed; winner margin +0.087 over ROY-B; escrow 0 after settle.
  - score factors: ROY-A 0.346, ROY-B 0.416, ROY-C 0.503; 40/40/20.
  - burned: 0 (W31 rework semantics keep it zero even on NO_SHOW).

## 3. Four completed auction lots (live chain evidence)
- At least three `Completed` lots independently verify the flow (`roy-1794152630606`, `roy-1794153275795`, `roy-1794153995214`), all winner ROY-C with 0.60N/0.40N payout and escrow 0.
- `smk-5-1794136566619`: also completed; `w31-1794138117517`: anti-slash regression status Failed, rework 1.
- Six-transaction success-tx set retained in W24 log (all FINAL).
- Lot tx IDs not repeated here; detailed reports already contain them (`W24`).

## 4. Farm score / static evidence
- `farm-score.json` snapshot fixed at `docs/evidence/farm-score.json`.
- Latest agreed metrics: highest `SPARK 8.54`, `FORGE 7.92`, `NEO2 7.08`, `CODEX 4.77`. The steady-state note: “latest wave changes the score, e.g. 7.75 -> 7.88”.
- This is derived club-run metadata, not on-chain.

## 5. Video / submission package
- `docs/demo/nexus-demo-final.mp4` (99s recorded testnet auction) and page links per `dfdcd54`.
- README novelty/maturity blocks from `7932896`.
- External claims remain bounded to local/testnet scope; no mainnet claim anywhere in this packet.

## 6. Honest static-vs-live split
- **Live-chain facts**: deployed contract, view-account code hash, get_agent x3, get_escrow=0, get_bid_scores winner margin +0.087, cancellation refunds, six-tx demo set, W31 on-chain no-slash.
- **Static/doc facts**: build/test counts, source majors, judged docs, farm-score computation (governance-like fold without a contract yet), smoke scripts, DRYRUN map, publication page.
- **Unsupported/unverified in this pass**: GitHub Pages ended with HTTP 200, but README line counts were not enumerate-claim-for-claim. No hidden blockers discovered.
- **Claim control**: stale wording remains only in historical audit/research queues, clearly dated. No pending-deployment claim in active submission path after the FORGE cleanup.

## Rules respected
No code changes, no builds, no commits, no push, no secrets.
