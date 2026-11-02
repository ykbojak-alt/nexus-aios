# NEAR Hackathon Submission Checklist

Status: draft. Values marked `TBD` must be supplied or confirmed before submission. No API request has been sent.

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
| `idea` | Draft below; 568 characters (required: 20–2000) | ☑ |
| `consent` | Pending explicit human confirmation of the exact contact notice | ☐ |

### Idea draft

NEXUS AiOS coordinates role-specialized agents as a task-focused swarm. An Architect decomposes work and hires specialized workers such as a research agent, coder, and auditor. LoopGuard checks agent actions against a role-based command policy and records allow/deny decisions. In the hackathon MVP, verified work settles on NEAR Testnet through the nexus-escrow contract: the manager deposits a budget, agents submit ed25519-signed bids that the contract verifies, and verify_and_release pays the winning worker on-chain. The demo prints real TxHashes for every step.

## Request ID generated at send time

- Generate one UUID immediately before submission and reuse the same value for retries after network errors or HTTP 503.
- This checklist does not store a requestId value; the API requires a client-generated UUID at send time.

## API contract checklist

Verified against the hackathon API spec: `POST https://nearhackathon.com/api/applications`, `Content-Type: application/json`, maximum body size 12000 bytes. Send exactly the fields below; do not add `website` or other fields. Submission remains unsent until the human reviews the complete payload and explicitly confirms it.

### Candidate request payload fields

```json
{
  "requestId": "GENERATE_AT_SEND_TIME",
  "projectName": "NEXUS AiOS",
  "leadName": "YKSunder",
  "email": "yk.bojak@gmail.com",
  "size": "1",
  "tracks": ["swarms", "security", "agents"],
  "nearAddress": "yk.sunder.near",
  "projectUrl": "https://github.com/ykbojak-alt/nexus-aios",
  "demoUrl": "https://ykbojak-alt.github.io/nexus-aios/demo/",
  "idea": "NEXUS AiOS coordinates role-specialized agents as a task-focused swarm. An Architect decomposes work and hires specialized workers such as a research agent, coder, and auditor. LoopGuard checks agent actions against a role-based command policy and records allow/deny decisions. In the hackathon MVP, verified work settles on NEAR Testnet through the nexus-escrow contract: the manager deposits a budget, agents submit ed25519-signed bids that the contract verifies, and verify_and_release pays the winning worker on-chain. The demo prints real TxHashes for every step.",
  "consent": false
}
```

This is a review draft, not a submit-ready payload: replace `GENERATE_AT_SEND_TIME` with one UUID at send time and obtain explicit human agreement to the exact contact notice before changing `consent` to `true`. The project name is now spelled `NEXUS AiOS` consistently; name availability still needs confirmation because the API requires a unique project name.

### Response handling from the official contract

- `201`: saved; report success to the human.
- `400`: inspect response `fields` for validation failures; fix and retry with the same requestId.
- `403`: submissions closed or request considered cross-site; do not retry.
- `409`: project name or email already used; ask the human, do not change identity to bypass it.
- `413`: body too large; shorten the idea.
- `503`: temporary storage failure; wait one minute and retry with the same requestId.
