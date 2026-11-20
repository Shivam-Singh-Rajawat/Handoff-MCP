# Handoff-MCP project status

Updated: 20 November 2026.

**Completed milestone: deployed web workspace with durable history and live
cross-model handoffs.** Native application integration remains in validation.

## Completed

| Area | Result and evidence |
| --- | --- |
| Hosting | Render Docker service deployed and live |
| Providers | Real GPT-OSS via Groq and Gemini calls completed in both lead directions |
| Web login | Google sign-in configured; user reported successful login and workspace access |
| Storage | Neon PostgreSQL stores history, projects, artifacts, queues, budgets and OAuth records |
| Persistence | Completed live run retrieved after container redeployment; user confirmed history after refresh |
| Jobs | Persistent queue/status, retry records and worker leases to prevent overlapping deploy workers |
| Limits | Workspace request limits, token budgets and concurrency controls implemented with automated coverage |
| MCP authorization | Discovery, dynamic registration, PKCE, explicit consent, rotating refresh tokens and revocation implemented |
| OAuth fixes | Same-origin referrer policy and separate short-lived Google consent cookie deployed |
| Tests | 27 automated tests passed; callback redirect/cookie smoke check passed with synthetic Google identity |
| Offline evaluation | 72 scored synthetic benchmark trials in earlier verification; not a live-quality benchmark |

## Live evidence

- Web: https://model-handoff-mcp.onrender.com/
- MCP: https://model-handoff-mcp.onrender.com/mcp
- User's GPT-OSS → Gemini → GPT-OSS test returned 42, with draft 505 ms,
  review 1900 ms and final 570 ms. This verifies orchestration for that simple task.
- A Gemini-led synthetic persistence test returned 42 and remained retrievable
  after redeployment. Both runs appeared in the user's recent history.
- Latest deployed code at this status update: `566b7564ea8fd150addf6b13c6176518a85689e8`.

## Remaining validation and implementation

- [ ] Complete ChatGPT OAuth retry and run a real MCP tool from ChatGPT.
  Earlier live attempts hit origin rejection, then expired consent. Fixes are
  deployed, but successful authorization has not yet been observed.
- [ ] Configure and test native Claude and Gemini clients in eligible accounts.
- [ ] Establish a scheduled backup/export and test PostgreSQL restoration.
  Durable database storage and redeployment persistence are verified; a backup
  restore drill has not been completed.
- [ ] Implement image workflows, Drive and general external MCP connectors.
- [ ] Add browser project-management screens and binary file storage.
- [ ] Prepare, scan and submit an Agensi skill ZIP; no listing has been published.

## Operational limits

Render remains on its free plan and can incur cold starts. Database/provider
quotas still apply. The configured GPT backend is GPT-OSS via Groq, not the
ChatGPT subscription model. Google workspace access is invitation-only; a public
URL does not grant visitors permission to use the owner's API credentials.
Automated checks are evidence of the tested behavior, not a guarantee of no bugs.

Never commit API keys, database URLs, workspace tokens or OAuth client secrets.

## Naming and compatibility

The project is named **Handoff-MCP**. The Python distribution is `handoff-mcp`;
the import package and existing `handoff` CLI stay compatible. The currently
deployed hostname remains `model-handoff-mcp.onrender.com` to preserve Google
redirect URIs, OAuth issuer/resource bindings and existing client configurations.
Repository and connected-client display names must be renamed in their respective
account settings when administrative access is available; this source rename
does not rewrite previously registered third-party clients or historical records.

Rename validation: the renamed package installs successfully. Seven native OAuth
tests pass. A Windows/Python 3.14 full-suite comparison produced the same two
backup cleanup errors and two evaluator failures before and after the rename;
these existing platform failures are not counted as passing tests.
