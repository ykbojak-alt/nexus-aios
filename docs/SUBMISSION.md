# NEAR Hackathon Submission Checklist

Status: draft. Values marked `TBD` must be supplied or confirmed before submission. No API request has been sent.

## Submission fields

| Field | Value / requirement | Status |
|---|---|---|
| `projectName` | `NESUS AiOS` | ☑ |
| `leadName` | `YKSunder` | ☑ |
| `email` | `yk.bojak@gmail.com` | ☑ |
| `size` | `2 - 4` (team: User + A.L.I.C.E. + agent swarm as collaborators) | ☑ |
| `tracks` | `swarms`, `security`, `agents` | ☑ |
| `nearAddress` | `yk.sunder.near` | ☑ |
| `projectUrl` | `https://github.com/ykbojak-alt/nexus-aios` | ☑ |
| `demoUrl` | TBD | ☐ |
| `idea` | Draft below; must be 20–2000 characters | ☐ validate length |
| `consent` | `true` | ☑ |

### Idea draft

NEXUS AiOS coordinates role-specialized agents as a task-focused swarm. An Architect decomposes work and hires specialized workers such as a research agent, coder, and auditor. LoopGuard checks agent actions against a role-based command policy and records allow/deny decisions. The hackathon MVP demonstrates verified work flowing through a NearIntentPayer stub that emits a settlement-shaped receipt for the worker. The current demo is local and uses a payment stub; it does not claim a live on-chain settlement.

## Request ID placeholder

- `requestId`: `<assigned-by-submission-service>` — preserve the value returned by the service for status checks and support.
- If the API requires a client-generated idempotency/request identifier instead, confirm the required format and generate it at submission time. Do not submit this placeholder literally.

## API contract checklist

The exact endpoint, method, authentication, and canonical request/response schema have not been verified in this repository. Confirm them against the official hackathon submission API before sending.

### Candidate request payload fields

```json
{
  "projectName": "NESUS AiOS",
  "leadName": "YKSunder",
  "email": "yk.bojak@gmail.com",
  "size": "2 - 4",
  "tracks": ["swarms", "security", "agents"],
  "nearAddress": "yk.sunder.near",
  "projectUrl": "https://github.com/ykbojak-alt/nexus-aios",
  "demoUrl": "<TBD>",
  "idea": "<20–2000 characters>",
  "consent": true
}
```

Confirm whether `requestId` belongs in this body or is returned by the service. Also confirm whether field names, `size` enum, and `consent` representation match the API schema.

### Response fields to capture (names/shape TBD until verified)

- HTTP status and success/error indicator.
- Service-assigned `requestId` or submission ID.
- Validation errors, if rejected.
- Submission status and any receipt/reference URL.

Do not treat this checklist as an API schema until those response keys and endpoint details are confirmed.
