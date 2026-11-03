# NEAR Hackathon Submission Checklist

Status: **submitted 2026-11-03** — HTTP 201, project visible in the public catalog as #81.

## Submission fields

| Field | Value / requirement | Status |
|---|---|---|
| `projectName` | `NEXUS AiOS` | ☑ |
| `leadName` | `YKSunder` | ☑ |
| `email` | `yk.bojak@gmail.com` | ☑ |
| `size` | `1` (human team size; AI agents are not team members) | ☑ |
| `tracks` | `swarms`, `security`, `agents` | ☑ |
| `nearAddress` | `yk.sunder.near` | ☑ |
| `projectUrl` | `https://github.com/ykbojak-alt/nexus-aios` | ☑ |
| `demoUrl` | `https://ykbojak-alt.github.io/nexus-aios/demo/` | ☑ |
| `idea` | 1074 chars (required: 20–2000) | ☑ |
| `consent` | Confirmed — contact notice approved by User before send | ☑ |

### Idea draft

NEXUS AiOS coordinates role-specialized agents as a task-focused swarm. An Architect decomposes work and hires specialized workers such as a research agent, coder, and auditor. LoopGuard checks agent actions against a role-based command policy and records allow/deny decisions. In the hackathon MVP, verified work settles on NEAR Testnet through the nexus-escrow contract: the manager deposits a budget, agents submit ed25519-signed bids that the contract verifies, and verify_and_release pays the winning worker on-chain. The demo prints real TxHashes for every step.

## Request ID (sent)

- `requestId`: **`f206fa10-94e3-4537-a8e6-912a712697a9`** (UUID v4, generated at send).
- Send result: `POST /api/applications` → **HTTP 201 (saved)**, one send, no retries.
- Verification: `GET /api/projects` → total 80 → **81**, project visible in the public catalog with tracks `["swarms","security","agents"]`.
- Payload: `D:\Temp\submit-payload.json` (1456 bytes; idea 1074 chars).

## API contract — submitted payload

Verified against the hackathon API spec: `POST https://nearhackathon.com/api/applications`, `Content-Type: application/json`, maximum body size 12000 bytes. Exactly the fields below; no `website` or other fields.

### Sent payload (as stored)

```json
{
  "requestId": "f206fa10-94e3-4537-a8e6-912a712697a9",
  "projectName": "NEXUS AiOS",
  "leadName": "YKSunder",
  "email": "yk.bojak@gmail.com",
  "size": "1",
  "tracks": ["swarms", "security", "agents"],
  "nearAddress": "yk.sunder.near",
  "projectUrl": "https://github.com/ykbojak-alt/nexus-aios",
  "demoUrl": "https://ykbojak-alt.github.io/nexus-aios/demo/",
  "idea": "AI agents can already research, write code, and review work — but they cannot pay each other for verified results. NEXUS AiOS solves this with a multi-agent swarm on NEAR Intents. An Architect decomposes a user task and auctions stages to specialized workers (research agent, coder, auditor); the cheapest cryptographically verified bid wins. The Research Agent runs a live Plan-Act-Observe loop: it queries an LLM, searches via Tavily, and synthesizes answers with [Source: URL] citations. Once cite_check verifies every citation, the nexus-escrow contract on NEAR Testnet releases payment to the worker: create_task deposits the budget, submit_bid carries an ed25519-signed price verified on-chain, and verify_and_release pays the winner. LoopGuard enforces role-based command allowlists and logs every allow/deny decision. The demo prints real TxHashes for every contract step. The Router/Worker split matches the \"super-agent\" architecture DeepSeek published days before this hackathon; we add the layer the paper leaves open — agents that settle verified work on-chain.",
  "consent": true
}
```

The final `idea` (1074 chars) is the one above; the earlier shorter draft is superseded.

### Response handling from the official contract

- `201`: saved; report success to the human.
- `400`: inspect response `fields` for validation failures; fix and retry with the same requestId.
- `403`: submissions closed or request considered cross-site; do not retry.
- `409`: project name or email already used; ask the human, do not change identity to bypass it.
- `413`: body too large; shorten the idea.
- `503`: temporary storage failure; wait one minute and retry with the same requestId.
