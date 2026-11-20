# Build brief for Codex · Multimodal MCP Workspace

## 1. Product and first release

Build a private, deployable workspace where GPT and Gemini can each lead a project, ask the other for help, use authorized tools, and incorporate the returned text, data, or file into the final answer. A user can resume the same project with either model.

Start from the attached **Handoff-MCP source archive**. Inspect the code and run its tests before changing it. The prototype has a Python MCP server, a two-way CLI, SQLite history and retry, provider adapters, and a scripted 12-task benchmark. Preserve working behavior where practical. Treat its offline results as regression evidence. Verify live behavior separately.

Deliver the implemented repository, Docker Compose configuration, environment template, migrations, automated tests, operating instructions, and verification results. The web workspace and remote MCP server are both deliverables. Package the software for deployment. Public hosting and publication require an explicit destination and authorization.

| Decision | First release |
|---|---|
| Models | GPT through the OpenAI API and Gemini through the Gemini API. Either can lead or help. |
| Clients | A small private web workspace for the complete workflow, plus Codex as a real MCP client. Keep local stdio for development. |
| Identity | Sign in with Google to the workspace. Link compatible MCP clients to the same workspace identity through OAuth. |
| Delegation | The user selects the lead and helper. Allow one helper round per task and a bounded number of tools. |
| Content | Project notes, PDFs, DOCX, TXT, Markdown, CSV, PNG, JPEG, and WebP. Return text, structured data, or downloadable files. |
| Tools | Read-only Drive, a built-in CSV statistics tool, and one explicitly configured external MCP server exercised end to end. |
| Deployment | Single instance with a small worker pool, PostgreSQL, and private S3-compatible object storage. |
| Later extensions | Claude adapter, direct native chat integrations where supported, additional connectors, image editing, and policy-based routing. |

Google sign-in grants access to the bridge account. Provider API access and billing use separate server credentials. Start with a project-owner access model. A dashboard for the first release can cover projects, uploads, handoffs, artifacts, and connected tools.

## 2. User journeys

### A. Continue a project in either model

Sign in, create a project, add a goal and notes, upload files, and start a task with GPT. Save the final answer and any artifacts. Later, open the project with Gemini and select the prior task or artifact as context. Show which notes and file versions are included.

### B. GPT delegates to Gemini and resumes

Upload a PDF and CSV. Ask GPT to draft a brief and delegate a source check to Gemini. Review the proposed handoff and destination before it is sent. Gemini returns findings with page or row references. GPT receives the actual helper result, revises the brief, and saves the final Markdown and review data. Show both model roles in history.

### C. Gemini delegates to GPT and resumes

Give Gemini a screenshot and a project brief. Ask it to identify issues, then delegate a test-plan draft to GPT using the screenshot findings and selected requirements. Gemini integrates the returned plan and saves a reusable artifact. Demonstrate that the returned plan is present in the lead's synthesis request.

### D. Use a tool and share its actual output

Pick a Drive document or project CSV. Let the lead call the authorized tool. Normalize the result and store it with provenance. Pass its selected pages, rows, structured result, or artifact to the other model. Show the requested action, connection, inputs, result, and which model consumed it.

### E. Generate an image and review it

Generate an image with a configured image-capable provider. Store the returned image bytes privately along with its metadata. Ask the other provider to review the saved image against the brief. A user can download the generated file and later use it as a project input. Route only to a model configured for the requested operation.

### F. Import an output from a host-only plugin

Upload a file or paste text produced in another chat app. Store its declared source and original format. Share that artifact from the workspace. For tools available through an authorized remote MCP endpoint or an official service API, use the direct integration.

## 3. Architecture

- Use Python, FastAPI, and the official MCP Python SDK. Share domain services between the web API, worker, CLI, and MCP tools.
- Use PostgreSQL for users, projects, metadata, approvals, jobs, step attempts, and usage. Use versioned migrations. Use private S3-compatible storage for bytes and store object keys and checksums in the database.
- Serve a small web UI with project navigation, file uploads, a task composer, selected-context preview, approvals, a step timeline, and downloads. Use polling or server events for job progress.
- Use one durable worker mechanism. A PostgreSQL-backed queue is sufficient if it supports atomic claims, leases, crash recovery, and bounded concurrency. Persist the state before calls that can take time or incur cost.
- Use provider adapters for GPT and Gemini. Normalize text, structured data, and binary artifacts while retaining provider model ID, operation, usage, and error classification. Validate configured model capabilities at startup or through a documented readiness check.
- Mount a Streamable HTTP MCP endpoint. Keep stdio as a local development and testing path. Verify compatibility with the target Codex version through a real tool call before release.
- Keep a single deployment topology for v1. Run the web/API, worker, database, and object storage with Docker Compose locally. Document a production equivalent using HTTPS, persistent storage, and secrets.

### Core records

| Record | Minimum fields |
|---|---|
| User | Internal ID, verified identity issuer and subject, display email |
| Project | Owner ID, title, goal, notes, timestamps |
| Artifact | Project, version, kind, media type, size, checksum, object key, source, parent references |
| Task | Project, lead, helper, prompt, selected artifact versions, status, final output references |
| Step attempt | Task, operation, input references, output references, timestamps, status, error category, idempotency key, previous attempt |
| Approval | User, task, destination, exact action and input fingerprint, decision, expiration |
| Connection | Owner, provider or server ID, granted scopes, secret reference, status |
| Usage | Step, provider model, token counts if reported, elapsed time, estimated cost or unknown |

All project-bound lookups must check the authenticated actor's access, including lookups by artifact ID, handoff ID, or download link creation. Freeze input references to specific artifact versions so retries and history reflect what was actually selected.

## 4. Workflow, files, and tool contracts

### Workflow execution

1. Create the task with selected project context and attachments.
2. Let the lead draft or propose a specific helper subtask or tool call.
3. Show the destination, exact operation, and selected context. Bind approval to that action and input fingerprint. Recheck access at execution time.
4. Run the tool or helper call. Store its status and normalized output.
5. Give the lead the helper result and relevant tool artifacts for synthesis. Save the final answer and link its sources.
6. Expose failure status and a retry action. Preserve finished steps and reuse them where their inputs still match.

Use structured states for queued, running, awaiting approval, completed, failed, cancelled, and outcome unknown. Use an idempotency key to deduplicate submissions and prevent simultaneous retries of the same step. Treat upstream timeouts with uncertain outcomes separately from confirmed failures. Retry external writes only if the upstream service can safely deduplicate them or the user has reconciled the prior attempt. If a model call may already have been charged, show that before a retry. Apply job and per-project usage limits.

### Content handling

- PDF. Preserve page references. For scanned pages, use a supported vision workflow or surface that OCR is required.
- DOCX. Extract paragraphs, headings, and supported tables with section references. Report content that cannot be extracted.
- Text and code. Accept UTF-8 text and use line ranges when practical. Return patches or code artifacts for review.
- CSV. Validate parsing and size. Support deterministic row counts, missing values, and simple summary statistics. Cite the columns or sample rows used.
- Images. Validate dimensions, size, and media type. Route image analysis and generation only to compatible models. Strip unnecessary metadata from images shared with providers where practical.
- Files. Set upload and extracted-size limits. Validate file types server-side. Store outputs privately and return short-lived download links after authorization. Track what was shared by reference. Keep content and credentials out of routine logs.

### Cross-model tool execution

Implement a registry with tool name, version, input schema, result schema or media type, authorization requirements, and whether it reads or changes external state. Validate model-supplied arguments. Make approval meaningful by binding it to the exact destination, operation, and input. Start with read-only external operations. Add write actions only with explicit approval and safe recovery.

Start with a read-only Google Drive connector, the built-in CSV statistics tool, and a controlled external MCP server configured by the operator. Test remote discovery and invocation with a known server. Restrict outbound remote tool connections to approved destinations. Keep provider and tool credentials scoped to the relevant connection. Treat tool descriptions, files, and outputs as data from external sources. They cannot grant permissions or expand the workflow's allowed actions.

Normalize tool output as text, structured data, or an artifact. Pass the next model selected content or bytes when its provider supports them. A file name or private URL alone is insufficient to demonstrate file transfer. Retain provenance and permissions when copying a tool result into a project.

### MCP interface

| Tool | Expected result |
|---|---|
| get_project_context | Selected project notes and recent task or artifact references |
| list_project_files | Authorized metadata and artifact IDs with pagination |
| get_file_excerpt | Bounded text, page, line, or row selection with provenance |
| create_handoff | Durable task ID, status, and approval URL or instructions where needed |
| get_handoff | State, step summaries, errors, and authorized result references |
| retry_handoff | Linked attempt ID for an eligible failed step |
| get_artifact | Metadata, excerpt, or short-lived authorized download link |
| list_project_tools | Enabled tools and their capabilities |
| run_project_tool | Job ID, approval status, and eventually artifact references |

Use structured outputs and stable error codes. The server authorizes tool calls independently of model input. For remotely approved actions, use the signed-in web approval flow or an equivalent verified user interaction. A model-provided confirmation field alone does not establish approval.

## 5. Delivery sequence

1. **Foundation.** Verify the prototype. Refactor shared services. Add migrations, storage, Google login, project ownership, and a local Compose stack. Exercise restart and backup/restore.
2. **Core workflow.** Ship both live model directions, durable attempts, approvals, retries, timeline, and text artifacts. Keep the CLI useful for reproducible evaluation.
3. **Multimodal workflows.** Add document, CSV, and image ingestion, image generation, downloads, and reuse of generated artifacts across providers.
4. **Tool workflows.** Ship Drive, CSV statistics, external MCP discovery and invocation, and manual import. Show cross-model use of real tool output.
5. **Remote MCP and release.** Verify the authorization flow and actual Codex tool invocation. Run deployment and live tests. Document operational setup and release limitations.

## 6. Release acceptance

| Check | Evidence to deliver |
|---|---|
| Clean startup and deployment | Start the stack from a fresh checkout using the documented commands. Run migrations and health checks. Record versions and exact commands. |
| Google identity and isolation | Sign in, create a project, and link a compatible MCP client to the same account. Verify that a second user cannot list or fetch tasks, files, or artifacts, including by guessed or copied IDs. |
| Two-way delegation | Run GPT → Gemini → GPT and Gemini → GPT → Gemini with live providers. Record step traces showing the helper result was supplied to the lead. |
| Multimodal | Run a PDF check with page references, a CSV analysis, a screenshot analysis, and an image generation plus review. Download a generated artifact after restarting the stack. |
| Tool handoff | Run a selected Drive PDF through a model-to-model workflow. Call the external MCP test tool and show the returned artifact used by the other model. Import a result from a host-only plugin and reuse it. |
| Resilience | Test timeout, duplicate request, worker restart, cancellation where supported, expired connection, denied artifact access, unsupported input, and a helper suggestion that fails verification. |
| Real MCP client | Use Codex to discover tools, create a handoff, and fetch its result over the deployed MCP transport. Keep an independent SDK smoke test for CI. |
| Operational readiness | Demonstrate migrations, health/readiness checks, secret setup, HTTPS configuration, storage persistence, backup/restore, and rollback steps. |

### AIML evaluation

Keep the 12 reproducible Python data tasks and independent assertions. Compare a single answer, same-model review, and cross-model review for both lead models. Use comparable inputs and call budgets for the two review conditions. Record task pass rate, assertion results, end-to-end time, and estimated API cost. Label scripted fixtures separately. Report an example where a helper added value if observed, a case where it did not, and how the system dealt with incorrect advice. Keep generated code execution confined to the isolated evaluator.

### Handoff to the project owner

Provide a README with copyable setup commands, required environment variables, data flow, deployment and rollback instructions, and a short demo script. Include an environment template without secrets. Identify any checks blocked by missing API credentials, OAuth setup, Docker, or hosting. Report offline, mocked, and live results separately.

Use the defaults in this brief. Ask the project owner before changing a core decision such as dropping either provider, replacing Google sign-in, changing the privacy model, enabling external writes, or deploying publicly. Routine implementation choices can proceed and should be documented. Ask for credentials or hosting details only when a live check depends on them.
