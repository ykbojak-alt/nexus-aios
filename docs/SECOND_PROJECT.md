# Second submission — HiveBid

The second project submitted under hackathon rule **Q-015** ("one person, several
projects?"): one participant may enter **up to two** projects, provided they are
independent **and each README names the other one**.

| | Submission №1 | Submission №2 |
|---|---|---|
| Project | **NEXUS AiOS** | **HiveBid** |
| Repository | this repository | <https://github.com/ykbojak-alt/hivebid> |
| Product | agent-orchestration platform: research agent, escrow settlement, LoopGuard policy gate | on-chain swarm auction market: escrow, weighted bid scoring, ratings, slashing |
| Contract | `contracts/nexus_escrow` — `nexus-escrow.yksunder.testnet` | `contracts/hivebid_market` — `hivebid-yksunder.testnet` |
| Demo recording | `docs/demo/hackathon-demo.mp4` + the published demo page | `docs/demo/nexus-demo-voiced-final.mp4` + its own published demo page |

## Why they are independent

- **Different products.** NEXUS AiOS hires and pays individual specialised agents
  and settles through `nexus-escrow` with an ed25519-verified bid. HiveBid is a
  market where *swarms* compete for a lot and the contract itself scores the
  bids.
- **Separate repositories, separate contracts, separate tests, separate demo
  recordings.** This repository contains no HiveBid code and no HiveBid media;
  the HiveBid repository contains no NEXUS code and no NEXUS media.
- **The disclosure is in both READMEs.** Each README names the other project and
  links to its repository, as the rule requires.

## Platform rules this submission follows

- Development window: 1–14 Nov 2026; submission deadline 14 Nov 2026, 23:59 UTC;
  results by 18 Nov 2026.
- The repository is **public**, the demo post is **public**, and both links are
  in the submission form.
- One submission per project.
- The demo post states the project was built at NEAR HACKATHON, tags
  @NEARProtocol and #NEARHackathon, and attaches the demo video.
- Testnet only: no mainnet accounts, no mainnet transfers, no key material in
  either repository.
