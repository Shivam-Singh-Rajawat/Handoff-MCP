# Model Handoff MCP

A Python MCP server and CLI that let GPT and Gemini delegate work to each other. The built-in demo fixes small Python data transformations and scores the final candidate against expected outputs.

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
with visitors. This MVP is a single-operator workspace and does not implement
Google sign-in or multi-user project isolation.

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
codex mcp add model-handoff -- "$(command -v handoff)" --offline --db "$PWD/.handoff/history.sqlite" serve
codex mcp get model-handoff
```

In PowerShell, use `(Get-Command handoff).Source` for the executable and an absolute database path. Open a fresh Codex session and ask:

> Use the model-handoff MCP server to ask Gemini for edge cases for a Python function that drops None but keeps zero. I authorize sending that synthetic example. Then retrieve the handoff by ID and show its status.

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

The Docker image runs a single-tenant HTTP server with a persistent SQLite database and a shared bearer token. For a hosted instance, terminate HTTPS at your reverse proxy, pass the expected Host header, and keep the backend port private. Any client with the token can read handoffs and incur provider charges. Use one instance and token per trusted workspace. Google sign-in and multi-user isolation can be added when a hosted multi-user workspace is required.

```bash
docker build -t model-handoff .
docker volume create model-handoff-data

export HANDOFF_HTTP_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export HANDOFF_HTTP_HOSTS="localhost:8000,127.0.0.1:8000"

docker run --rm -p 127.0.0.1:8000:8000 \
  --mount source=model-handoff-data,target=/data \
  -e HANDOFF_HTTP_TOKEN -e HANDOFF_HTTP_HOSTS \
  -e OPENAI_API_KEY -e OPENAI_MODEL -e GEMINI_API_KEY -e GEMINI_MODEL \
  -e OPENAI_INPUT_USD_PER_MILLION -e OPENAI_OUTPUT_USD_PER_MILLION \
  -e GEMINI_INPUT_USD_PER_MILLION -e GEMINI_OUTPUT_USD_PER_MILLION \
  model-handoff

# In the Codex shell, export the same HANDOFF_HTTP_TOKEN before starting Codex.
codex mcp add model-handoff-http --url http://127.0.0.1:8000/mcp \
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
The token grants access to one shared workspace. Google sign-in, separate visitor
accounts, and OAuth discovery for generic remote MCP clients are future work.

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
   codex mcp add model-handoff --url https://YOUR-SERVICE.onrender.com/mcp \
     --bearer-token-env-var HANDOFF_HTTP_TOKEN
   ```

   Ask Codex to list the tools, then call `ask_model` with a synthetic task
   and explicitly approve the context sharing.

Render provides the port and hostname automatically. Free services spin down
after inactivity. SQLite history survives within an instance but can be erased
on restart or redeploy. Export runs as JSON if you need to keep them. Use sample
data for the free-tier demo because Google may use free-tier Gemini inputs and
outputs to improve its products.

To smoke-test credentials locally, set `HANDOFF_GPT_BACKEND=groq`,
`GROQ_API_KEY`, `GROQ_MODEL`, `GEMINI_API_KEY`, and `GEMINI_MODEL` in your
shell, then run `handoff check-providers --confirm`. This submits one short
synthetic prompt to each provider and prints model IDs and latency. For the
direct OpenAI API, set `HANDOFF_GPT_BACKEND=openai` and use `OPENAI_API_KEY`
and `OPENAI_MODEL` in place of the Groq settings. ChatGPT Plus and OpenAI API
billing are separate. Groq cost estimates use optional
`GROQ_INPUT_USD_PER_MILLION` and `GROQ_OUTPUT_USD_PER_MILLION`.
