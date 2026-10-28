# NEAR Hackathon Submission Checklist

Status: draft. Values marked `TBD` must be supplied or confirmed before submission. No API request has been sent.

## Submission fields

| Field | Value / requirement | Status |
|---|---|---|
| `projectName` | `NESUS AiOS` | ☑ |
| `leadName` | `YKSunder` | ☑ |
| `email` | `yk.bojak@gmail.com` | ☑ |
| `size` | `1` (human team size; AI agents are not team members) | ☑ |
| `tracks` | `swarms`, `security`, `agents` | ☑ |
| `nearAddress` | `yk.sunder.near` | ☑ |
| `projectUrl` | `https://github.com/ykbojak-alt/nexus-aios` | ☑ |
| `demoUrl` | TBD | ☐ |
| `idea` | Draft below; 513 characters (required: 20–2000) | ☑ |
| `consent` | Pending explicit human confirmation of the exact contact notice | ☐ |

### Idea draft

NEXUS AiOS coordinates role-specialized agents as a task-focused swarm. An Architect decomposes work and hires specialized workers such as a research agent, coder, and auditor. LoopGuard checks agent actions against a role-based command policy and records allow/deny decisions. The hackathon MVP demonstrates verified work flowing through a NearIntentPayer stub that emits a settlement-shaped receipt for the worker. The current demo is local and uses a payment stub; it does not claim a live on-chain settlement.

## Request ID placeholder

- `requestId`: `00000000-0000-0000-0000-000000000000` — format placeholder only, not a generated request ID. Generate a real UUID once immediately before submission and reuse that value for retries after network errors or HTTP 503.
- Do not submit the placeholder literally. The API contract requires a client-generated UUID; it is not a service-assigned response field.

## API contract checklist

Verified against `D:\Downloads\HACKATHON.md`: `POST https://nearhackathon.com/api/applications`, `Content-Type: application/json`, maximum body size 12000 bytes. Send exactly the fields below; do not add `website` or other fields. Submission remains unsent until the human reviews the complete payload and explicitly confirms it.

### Candidate request payload fields

```json
{
  "requestId": "00000000-0000-0000-0000-000000000000",
  "projectName": "NESUS AiOS",
  "leadName": "YKSunder",
  "email": "yk.bojak@gmail.com",
  "size": "1",
  "tracks": ["swarms", "security", "agents"],
  "nearAddress": "yk.sunder.near",
  "projectUrl": "https://github.com/ykbojak-alt/nexus-aios",
  "demoUrl": "<TBD>",
  "idea": "NEXUS AiOS coordinates role-specialized agents as a task-focused swarm. An Architect decomposes work and hires specialized workers such as a research agent, coder, and auditor. LoopGuard checks agent actions against a role-based command policy and records allow/deny decisions. The hackathon MVP demonstrates verified work flowing through a NearIntentPayer stub that emits a settlement-shaped receipt for the worker. The current demo is local and uses a payment stub; it does not claim a live on-chain settlement.",
  "consent": false
}
```

This is a review draft, not a submit-ready payload: replace the requestId placeholder with one generated UUID, supply a public `demoUrl`, and obtain explicit human agreement to the exact contact notice before changing `consent` to `true`. The current `projectName` must also be confirmed: it is written as `NESUS AiOS` here while the repository README says `NEXUS AiOS`. The API requires a unique project name, which cannot be verified from this file.

### Response handling from the official contract

- `201`: saved; report success to the human.
- `400`: inspect response `fields` for validation failures; fix and retry with the same requestId.
- `403`: submissions closed or request considered cross-site; do not retry.
- `409`: project name or email already used; ask the human, do not change identity to bypass it.
- `413`: body too large; shorten the idea.
- `503`: temporary storage failure; wait one minute and retry with the same requestId.
