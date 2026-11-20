# Handoff-MCP

A Python MCP server and CLI that let GPT and Gemini delegate work to each other. The built-in demo fixes small Python data transformations and scores the final candidate against expected outputs.

## Deployment milestone completed — 20 November 2026

The web workspace is deployed at **https://model-handoff-mcp.onrender.com/**.
The authenticated remote MCP endpoint is **https://model-handoff-mcp.onrender.com/mcp**.

Completed and checked: live GPT-OSS via Groq ↔ Gemini handoffs, Google workspace
sign-in (user-confirmed), Neon PostgreSQL storage, saved history after refresh
and redeployment, persistent background jobs, request/token/concurrency limits,
and native MCP OAuth implementation. The latest automated run passed **27 tests**.

**Native ChatGPT connection is still awaiting a successful authorization retry
and first tool call.** Consent origin and cookie-lifecycle fixes are deployed;
passing protocol tests does not establish native-client success. Claude and
Gemini native application connections are also unverified.

See [STATUS.md](STATUS.md) for completed scope, evidence, and remaining work.

## Features

- **Two-way delegation.** Either model drafts a solution, asks the other to review it, and uses the review in its final answer.
- **Codex integration.** Connect over stdio locally or authenticated Streamable HTTP when deployed.
- **Persistent history and recovery.** Inspect the selected context, provider, reply, latency, usage, and failure status by handoff ID. Retrying a failed request creates a linked record and preserves the original.
- **Evaluation.** Compare one draft, a same-model review, and a cross-model review on 12 synthetic Python tasks. Scripted fixtures let you try the entire pipeline without credentials.

## Run the private web workspace

Install the project with `python -m pip install .`. Then start the server from the project directory:

```bash
export HANDOFF_HTTP_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
printf '%s\n' "$HANDOFF_HTTP_TOKEN"
handoff --offline serve --transport http
```

On Windows PowerShell, use:

```powershell
$env:HANDOFF_HTTP_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:HANDOFF_HTTP_TOKEN
handoff --offline serve --transport http
```

Open http://127.0.0.1:8000 and unlock with the token printed locally. Select the lead,
enter a task, and optionally upload a text-based PDF, CSV, TXT, or Markdown file
up to 2 MiB. The server extracts a preview. You can edit or remove parts before
approving a run. CSV previews include real row counts, missing values and a small
sample. Original file bytes are discarded after preview extraction. SQLite retains
the submitted context and the completed draft, review, and final answer.

Offline mode returns labeled fixtures. For two-way live calls, configure both
providers as described below and remove `--offline`. Keep the server single-process
and use the same private token for the web workspace and the MCP client. Anyone
holding the token has access to all saved runs on that instance. Do not share it
with visitors. Invite tokens and configured Google identities select isolated or explicitly shared workspaces.
See the 0.3 deployment section for persistent PostgreSQL storage and Google login.

The web request waits for up to three provider calls. On a reported provider
failure, restart the remaining steps from **Run details**. If the server process
is killed mid-request, the run can stay marked running. Inspect its recorded steps
before submitting a new run. For hosting, put the app behind HTTPS, persist
`/data`, set `HANDOFF_HTTP_HOSTS` to the expected host, and allow enough proxy
time for the combined provider calls. Publish the code separately from the
authenticated workspace.

## Install and run offline

Requires Python 3.11 or later. From this directory, run:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install .
python -m unittest discover -s tests -v
handoff --offline demo --lead gpt --task zero-is-data
handoff --offline demo --lead gemini --task complete-csv-fields
handoff --offline benchmark --output evidence/my-offline-run.json
```

On Windows PowerShell, activate with `.\.venv\Scripts\Activate.ps1`. Each demo prints the initial draft, review, final answer, test score, and handoff ID. Run `handoff get HANDOFF_ID` from the same directory to see the persisted request and response.

## Try the MCP server from Codex

With the virtual environment active, register the local server:

```bash
codex mcp add handoff-mcp -- "$(command -v handoff)" --offline --db "$PWD/.handoff/history.sqlite" serve
codex mcp get handoff-mcp
```

In PowerShell, use `(Get-Command handoff).Source` for the executable and an absolute database path. Open a fresh Codex session and ask:

> Use the handoff-mcp MCP server to ask Gemini for edge cases for a Python function that drops None but keeps zero. I authorize sending that synthetic example. Then retrieve the handoff by ID and show its status.

Offline mode returns a scripted answer and makes no provider calls. For a live local server, register the command without `--offline` and provide the model and API variables to the MCP subprocess. If your Codex configuration filters inherited environment variables, use its `env_vars` list or a local launcher that reads credentials from your secret store.

| Tool | Arguments |
|---|---|
| `ask_model` | `target` (`gpt` or `gemini`), `task`, `context`, `confirmed` |
| `get_handoff` | `handoff_id` |
| `retry_handoff` | `handoff_id`, `confirmed` |

Codex calls the MCP tools. The server calls the model APIs. The two-way CLI drives the draft, review, and final-answer steps through the same handoff service as the MCP tools. A helper receives a prompt and returns text. It has no access to this server's tools and cannot initiate a nested call.

## Run with live providers

Set `OPENAI_API_KEY`, `OPENAI_MODEL`, `GEMINI_API_KEY`, and `GEMINI_MODEL` in your shell or secret store. Use available model IDs from your own API projects. The API credentials and billing are separate from chat subscriptions. The service does not require a chat login.

For cost estimates, set `OPENAI_INPUT_USD_PER_MILLION`, `OPENAI_OUTPUT_USD_PER_MILLION`, `GEMINI_INPUT_USD_PER_MILLION`, and `GEMINI_OUTPUT_USD_PER_MILLION` to the rates for the selected models. The estimates use reported tokens and flat rates. They exclude factors such as cache discounts, tiered pricing, and failed calls with unavailable usage. Missing prices appear as JSON `null`.

To delegate a general text task, select a UTF-8 file containing only the excerpts you want both providers to receive:

```bash
handoff delegate --lead gemini --task "Review this data-cleaning plan and give me a final recommendation" \
  --context selected-excerpts.txt --confirm
```

For live demos and benchmarks, install and start Docker on the machine running the CLI. It executes generated code in short-lived, unprivileged containers with networking disabled, a read-only filesystem, and CPU, memory, process, and wall-time limits. The evaluator is for the supplied synthetic tasks in a trusted evaluation environment. Its assertions share a container with the candidate and are unsuitable as a security boundary against deliberately adversarial submissions.

```bash
docker pull python:3.12-slim
handoff demo --lead gpt --task zero-is-data --confirm
handoff demo --lead gemini --task complete-csv-fields --confirm
handoff benchmark --output evidence/live-benchmark.json --confirm
```

## Deploy the MCP server

The Docker image runs a single-tenant HTTP server with a persistent SQLite database and a shared bearer token. For a hosted instance, terminate HTTPS at your reverse proxy, pass the expected Host header, and keep the backend port private. Any client with the token can read handoffs and incur provider charges. Use one instance and token per trusted workspace. Configure native OAuth and invited Google identities as described in the 0.3 deployment section.

```bash
docker build -t handoff-mcp .
docker volume create handoff-mcp-data

export HANDOFF_HTTP_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export HANDOFF_HTTP_HOSTS="localhost:8000,127.0.0.1:8000"

docker run --rm -p 127.0.0.1:8000:8000 \
  --mount source=handoff-mcp-data,target=/data \
  -e HANDOFF_HTTP_TOKEN -e HANDOFF_HTTP_HOSTS \
  -e OPENAI_API_KEY -e OPENAI_MODEL -e GEMINI_API_KEY -e GEMINI_MODEL \
  -e OPENAI_INPUT_USD_PER_MILLION -e OPENAI_OUTPUT_USD_PER_MILLION \
  -e GEMINI_INPUT_USD_PER_MILLION -e GEMINI_OUTPUT_USD_PER_MILLION \
  handoff-mcp

# In the Codex shell, export the same HANDOFF_HTTP_TOKEN before starting Codex.
codex mcp add handoff-mcp-http --url http://127.0.0.1:8000/mcp \
  --bearer-token-env-var HANDOFF_HTTP_TOKEN
```

To smoke-test the container without keys, append `handoff --offline serve --transport http --host 0.0.0.0` after the image name. The server retains raw task text, selected excerpts, and replies in SQLite. Keep the database and backups private. The MCP `confirmed` flag records a client's declaration of user authorization. Use the client's tool approval controls when sharing sensitive project context.

If a provider call fails, retrieve the original record and run `handoff retry HANDOFF_ID --confirm`, or use `retry_handoff`. A timeout may occur after a provider received the request, so a retry can incur another charge. A process killed mid-call can leave a record marked `running`. Inspect it and submit a fresh request if needed.

## Read the benchmark

Each of 12 tasks has three assertions. The models see the requirement, original function, and one failing example. Two assertions per task are withheld. Each lead runs these conditions:

| Condition | Calls per task |
|---|---:|
| Single draft | 1 |
| Same-model draft, review, revision | 3 |
| Cross-model draft, review, revision | 3 |

The review and revision prompts match across the two three-call conditions. Each gets the same output token cap. The full live run makes up to **168 model calls** across 72 trials. The report records tasks fully passed, assertions passed, elapsed wall time, provider timings, token usage, and estimated cost where rates and usage are available. It writes progress after every trial. Provider errors and invalid candidates have explicit statuses and count as failed trials.

[evidence/offline-benchmark.json](evidence/offline-benchmark.json) contains one scripted run. The offline reviewer supplies a reference fix and the final fixture returns it. The fixture results exercise the plumbing and scoring. Live trials are needed to estimate whether cross-model review helps and what it costs. In live results, compare `same_model` against `handoff` for each lead and inspect individual trials where results differ.

## Upstream references

- [Official Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)
- [Codex MCP configuration](https://developers.openai.com/codex/mcp)
- [OpenAI Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [Gemini generateContent API](https://ai.google.dev/api/generate-content)

## Public walkthrough and private workspace

The landing page includes two interactive scripted examples. Visitors can
inspect the source evidence, lead draft, reviewer findings, and revised answer
without credentials. These examples run entirely in the browser and contain
synthetic data. The public `GET /healthz` endpoint checks that the app is serving
requests. Provider diagnostics and workspace data require the access token.

The private workspace has three modes for the same task and visible context.

| Mode | Provider calls on a successful run |
| --- | --- |
| Single pass | One answer from the lead |
| Same-model review | Lead draft, a fresh review from the same model, and lead revision |
| Cross-model review | Lead draft, review from the other provider, and lead revision |

To inspect the difference, submit the same prompt and excerpt with each mode,
then download the run JSON. Exports include the shared text and model responses
as well as per-stage model, latency and available usage fields. Treat them like
the original private inputs. The public walkthrough is scripted, and the offline
workspace uses fixtures. Use live runs or the CLI benchmark for model-quality results.

For an internet-facing deployment use HTTPS and keep the workspace token private.
The owner token opens the existing workspace. Add invite-only workspaces by
setting the optional `HANDOFF_ACCOUNTS` secret to a JSON object mapping names to
distinct random tokens, such as `{"reviewer":"<generated token>"}`. Generate each
token with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Give
each person only their own token. Browser history and remote MCP handoffs are
isolated in separate SQLite databases. Keep the account name when rotating its
token to keep its history. Remove the entry to revoke access on the next restart.
Native MCP OAuth and Google login are available with the 0.3 configuration below.

To import context, paste a public GitHub issue URL and select **Load issue into
context**. The app fetches the issue title and body with a read-only GitHub
request. Review the editable preview before sending it to either provider.
This connector uses GitHub's unauthenticated public API.

## Free hosted deployment

The included `render.yaml` runs the same Python web and MCP server on a
[Render Free web service](https://render.com/docs/free). Anyone can see the
scripted examples at the root URL. A generated private workspace token protects
uploads, live runs, history, and the MCP endpoint.

1. Push this directory to a GitHub repository. In the
   [Render dashboard](https://dashboard.render.com/), choose **New → Blueprint**,
   connect the repository, and select its root `render.yaml`.
2. Create a [Groq API key](https://console.groq.com/keys) and a
   [Gemini API key](https://aistudio.google.com/api-keys) from your accounts.
   Enter them in Render as `GROQ_API_KEY` and `GEMINI_API_KEY`. Use projects
   with free quota. The defaults are OpenAI GPT-OSS 20B hosted by Groq and
   Gemini 3.1 Flash-Lite. Confirm these models appear in your provider accounts.
3. Apply the Blueprint. Use the assigned HTTPS URL after the deploy becomes
   healthy. Run `curl --fail https://YOUR-SERVICE.onrender.com/healthz`, then
   open the root URL in a browser and try the public scripted examples.
4. Copy `HANDOFF_HTTP_TOKEN` from the service's Environment tab into the
   browser's unlock form. Run a synthetic task in both directions. Check
   that each result is completed and shows the intended model for all three
   steps. The provider keys remain on the server.
5. For Codex, export the same token as `HANDOFF_HTTP_TOKEN` in its environment
   and register the MCP endpoint:

   ```bash
   codex mcp add handoff-mcp --url https://YOUR-SERVICE.onrender.com/mcp \
     --bearer-token-env-var HANDOFF_HTTP_TOKEN
   ```

   Ask Codex to list the tools, then call `ask_model` with a synthetic task
   and explicitly approve the context sharing.

Render provides the port and hostname automatically. Free services spin down
after inactivity. SQLite history survives within an instance but can be erased
on restart or redeploy. Export runs as JSON if you need to keep them. Use sample
data for the free-tier demo because Google may use free-tier Gemini inputs and
outputs to improve its products.

For durable history, choose a paid Render web service and attach a persistent disk
at `/data` before creating live runs. The Docker image defaults to
`HANDOFF_DB=/data/history.sqlite`. Ensure the service user (UID 10001) can write
the mounted path and confirm the database and invited workspaces survive a
restart before uploading private material.

To smoke-test credentials locally, set `HANDOFF_GPT_BACKEND=groq`,
`GROQ_API_KEY`, `GROQ_MODEL`, `GEMINI_API_KEY`, and `GEMINI_MODEL` in your
shell, then run `handoff check-providers --confirm`. This submits one short
synthetic prompt to each provider and prints model IDs and latency. For the
direct OpenAI API, set `HANDOFF_GPT_BACKEND=openai` and use `OPENAI_API_KEY`
and `OPENAI_MODEL` in place of the Groq settings. ChatGPT Plus and OpenAI API
billing are separate. Groq cost estimates use optional
`GROQ_INPUT_USD_PER_MILLION` and `GROQ_OUTPUT_USD_PER_MILLION`.

## Collaboration release

Select GPT, Gemini, or Claude as the lead and a helper in the web workspace.
Claude requires `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL`. Model IDs must be
available in the corresponding provider account.

Runs submitted through the web UI or the `create_run` MCP tool return immediately
with a queued run ID. Poll `get_run` for progress and the final answer.
`retry_run` creates a new, linked attempt using completed stages from the failed
run. The browser polls automatically. Queued work resumes after restart.
Interrupted provider calls are marked failed for explicit retry because the
provider may already have charged for a response.

A teammate can share an existing workspace using their own token. For example,
configure the secret `HANDOFF_ACCOUNTS` as follows, replacing placeholders with
distinct randomly generated tokens:

```json
{
  "research": "<independent-workspace-token>",
  "teammate": {"token": "<collaborator-token>", "workspace": "research"}
}
```

The teammate sees the same projects, artifacts, runs, and budget as research.
Every collaborator has full access within that workspace. Removing their token
revokes access after restart. Independent workspaces remain isolated.
The owner workspace is named `owner`.

MCP clients can create and list projects, load saved context, save text artifacts,
and retrieve artifacts. Reports, extracted document text, and code can be reused
from another client using the same workspace. These project tools are available
over stdio and HTTP. Binary file storage and project-management browser screens
are pending.

### Limits and operation

Run one application process per data directory. The included `compose.yaml` runs
one container with a named data volume and a readiness health check. Set secrets
in your shell or an ignored `.env`, then use `docker compose up --build -d`.
Configure HTTPS at your reverse proxy before exposing it externally.

Run one application process per data directory. This release uses SQLite and a
worker thread per independent workspace. Set the following environment values:

| Variable | Default | Enforcement |
| --- | --- | --- |
| HANDOFF_DAILY_RUNS | 50 | New queued runs per workspace per UTC day |
| HANDOFF_PENDING_RUNS | 10 | Queued and running jobs per workspace |
| HANDOFF_DAILY_CALLS | 300 | All provider calls per workspace per UTC day |
| HANDOFF_DAILY_OUTPUT_TOKENS | 1228800 | Conservative output-token reservations |
| HANDOFF_MAX_OUTPUT_TOKENS | 4096 | Maximum generated tokens requested per call |
| HANDOFF_TIMEOUT_SECONDS | 45 | Provider HTTP timeout |

Each provider attempt reserves its entire output allowance even on failure.
Input size is capped through task and context limits. Output reservations and
call counts bound usage but do not provide a monetary billing cap.
All calls, including legacy synchronous MCP tools, pass the provider limits.
One provider call can execute at a time in each workspace. Prefer queued tools
for collaboration. The legacy tools can report `workspace_busy` during a run.

Use `/healthz` for liveness and `/readyz` to check database access and worker
availability. Provider credentials are verified separately using
`handoff check-providers --confirm`.

Create a consistent backup outside the data directory:

```bash
handoff --db /data/history.sqlite backup --destination /backups/release-2026-11-19
```

This snapshots the main database and independent workspace databases and checks
their SQLite integrity. Copy backups to durable storage and schedule the command
through the hosting platform. To restore, stop the application, replace its
database tree with the snapshot, retain the same account-to-workspace mapping,
and restart. Backups contain private task context and artifacts.

### OAuth for remote chat clients

The server can validate access tokens from a separately configured OAuth service.
That service handles login, consent, PKCE, client registration, and optionally
Google sign-in. Configure:

- `HANDOFF_PUBLIC_URL` — the external HTTPS origin.
- `HANDOFF_OAUTH_ISSUER` — the exact authorization-server issuer.
- `HANDOFF_OAUTH_JWKS_URL` — its HTTPS signing-key endpoint.
- `HANDOFF_OAUTH_SUBJECTS` — a JSON mapping from authorized subject IDs to
  configured workspace names, for example `{"user-id":"research"}`.

Tokens must use RS256, have the exact issuer, include the `handoff` scope, and
have an audience matching `HANDOFF_PUBLIC_URL + "/mcp"`.
The server publishes protected-resource metadata and advertises it in its
unauthorized response. Unlisted identities are denied. Configure the OAuth
service to support the intended clients and issue access tokens for this resource.

Codex and clients supporting bearer headers can use the workspace token directly.
ChatGPT, Claude, and Gemini application availability and authorization support
must be verified with the user's actual account. A successful protocol test
does not establish that all native applications can connect. Native Google sign-in is now configured and user-confirmed. An external OAuth
service is optional because the server includes native MCP OAuth.

### Verification record

The collaboration upgrade has automated coverage for queued execution, duplicate
submission, limits, restart handling, collaborator access, isolated accounts,
artifact persistence, backup restoration, Claude request parsing, and OAuth
token audience, scope, expiry, and subject checks. A separate HTTP process was
started and exercised through a real localhost MCP request using offline
providers.

The deployment milestone supersedes the earlier local-only verification:
real Groq/Gemini calls, hosted discovery, Google website login, deployed Docker
startup and PostgreSQL persistence have now been exercised. Native ChatGPT,
Claude and Gemini application tool calls remain unverified. Image analysis,
Drive and general external MCP connectors remain pending.

The latest test run passed 27 automated tests. Earlier evaluation included
72 scored offline benchmark trials; fixtures provide no evidence of live model
quality. See STATUS.md for the current release record.

## 0.3 deployment: persistent storage and native OAuth

Set `DATABASE_URL` to a pooled PostgreSQL connection URL in Render's secret
environment settings. Use TLS. All workspace history, projects, artifacts,
provider budgets, queues, OAuth clients and grants then live in PostgreSQL.
Separate database schemas isolate independent workspaces. Render can sleep or
redeploy while that external database retains the records. Without DATABASE_URL,
the previous SQLite behavior remains available for local use.

The database role must be permitted to create workspace schemas. Connections use
a small client pool. Keep one configured Render service instance. A database
worker lease prevents old and new deployment containers from consuming the same
queue concurrently. Interrupted provider calls require explicit retry.

To import an existing SQLite workspace, stop new writes, retain a copy, and run:

```bash
handoff --db /path/to/history.sqlite migrate-sqlite --workspace owner
```

The destination comes from DATABASE_URL. The import preserves IDs and skips
conflicting records. Import independent workspace databases separately using
their configured names. A local SQLite file must still exist to be imported.
For PostgreSQL backups, use pg_dump and the database provider's backup/restore
facilities. The SQLite backup command rejects PostgreSQL mode.

### Native MCP authorization

Set these non-secret environment variables:

```text
HANDOFF_NATIVE_OAUTH=true
HANDOFF_PUBLIC_URL=https://model-handoff-mcp.onrender.com
```

The server provides authorization-server discovery, protected-resource metadata,
dynamic client registration, explicit consent, PKCE S256, expiring access
tokens, rotating refresh tokens, and revocation. Native clients use this URL:

```text
https://model-handoff-mcp.onrender.com/mcp
```

Select OAuth and automatic client registration. Request `handoff offline_access`.
Users can authorize access with their existing workspace token on the consent
page. A separate Auth0 account is optional. The previously supported external
OAuth token-verification configuration remains an alternative.

Claude users should choose **Register automatically** for the OAuth client.
Configure the actual remote connector in the intended user's Claude account.
Codex and Gemini CLI can also connect using the remote MCP URL and an authorized
bearer token where their client supports fixed headers.

Native application availability is controlled by the application provider.
At verification time, Google's official Gemini web documentation requires a US
personal account. OpenAI's custom-MCP help page lists Business and
Enterprise/Edu availability. Check the actual account's controls. These account
requirements do not change Gemini API access through this workspace.

Official client guidance:
- https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt
- https://support.google.com/gemini/answer/17209137
- https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp

### Google sign-in

Create a **Web application** OAuth client in the Google Cloud project you
control. Configure its authorized redirect URI exactly as:

```text
https://model-handoff-mcp.onrender.com/auth/google/callback
```

Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in Render, and configure an
explicit invitation map in `HANDOFF_GOOGLE_WORKSPACES`:

```json
{"your-email@example.com":"owner"}
```

Map collaborators to an existing independent workspace name to share it.
Users outside this map are denied. While Google's OAuth app is in testing,
include each intended account in its test-user list.

Once configured, the browser shows **Continue with Google**. Google login also
works inside native MCP consent. Google proves identity, and a separate explicit
consent action grants a client workspace access. Web sessions use Secure,
HttpOnly cookies. Cookie-authenticated writes require the same origin.
Application access logging is disabled so OAuth callback query credentials
are excluded from application access logs. The app never receives the user's
Google password.

Tests cover registration, consent CSRF checks, PKCE mismatch, one-use codes,
refresh rotation, grant revocation, Google nonce and audience validation,
verified email and invitation checks, and overlapping worker leases. The real
Google OAuth client credentials and each native application account still
require live setup and end-to-end verification.
