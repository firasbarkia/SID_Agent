# SID Agent API

A recruitment AI backend using Python 3.11+, FastAPI, MongoDB and uv. Groq generates
letters with Gemini fallback. Phase 2 adds versioned profile/draft storage, candidate
approval, a leased background worker and shared account-wide circuit/quota controls.
See [Phase 2 setup and integration status](docs/phase-2.md).
Phase 3 adds public reference ingestion, pinned local multilingual embeddings,
versioned Qdrant indexing and protected reference search. See
[Phase 3 setup, data coverage and recovery](docs/phase-3.md).
Phase 4 adds private PDF CV imports, page evidence, editable review drafts,
exact-version confirmation and a deterministic profile completeness score. See
[Phase 4 setup and API contract](docs/phase-4.md).
The supplied conversation pipeline now has private history, context routing,
reference-based advice and keyword memory. Matching remains disabled pending
visibility rules. See [pipeline review and API contract](docs/pipeline-alignment.md).
The [Agent v1 facade](docs/agent-api.md) now exposes typed `/agent/parse-cv`,
`/agent/generate-pack` and `/agent/chat` contracts. The explicit local demo can
search synthetic offers; real private matching and canonical offer resolution
remain disabled pending verified platform contracts.
Application dossiers add quoted evidence, CV suggestions and source-bound approval
using candidate-supplied job/company text. See [Phase 5](docs/phase-5.md).
Conversation deletion, queue status, a non-root Docker image and integration CI
provide the [operations baseline](docs/operations.md). Platform login, canonical
offer binding and submission still require the main backend's contracts.

## Quick Streamlit test workspace

For the prepared simulation, run `uv run --group demo python -m scripts.simulation start`
with Docker Desktop running and the local embedding model installed. Open
http://127.0.0.1:8501 and select **Simulation** to run the behavior scenarios or
try prompts yourself. This launcher forces simulated AI and prepares references
automatically. See [simulation setup, scenarios and stop commands](docs/simulation.md).
The manual demo commands below also support optional live provider evaluation.

The local console exercises previews, profiles, PDF CV imports/corrections,
letters/dossiers, evidence edits, approvals, conversations, reference search,
history and operational status. It bypasses login with three synthetic identities
in a separate `sid_demo` database. Only the explicit `app.demo` entry point enables
this behavior; `app.main` retains its existing authentication requirements.
The API accepts localhost peers/hosts/origins only and uses dedicated demo platform
collections. Use the loopback-bound command below, without a reverse proxy.

Install the optional interface dependencies and start the existing local services:

```powershell
uv sync --group demo
docker compose -p sid-phase2 -f compose.mongodb.yml up -d --wait
docker compose -p sid-phase3 -f compose.qdrant.yml up -d
uv run python -m app.knowledge.cli download-model
uv run python -m app.demo
```

In a second terminal, start the console:

```powershell
uv run --group demo python -m streamlit run streamlit_app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false
```

Open http://127.0.0.1:8501. The demo API runs on port 8001 and starts draft and CV
workers automatically. Its default MongoDB/Qdrant ports match the compose files.
In **Reference search**, click **Prepare demo reference index** once to import a
small balanced reference subset and build a separate `sid_test_*` vector index.
In **CV import**, download and re-upload the supplied sample PDF. Refresh pending
CV imports or drafts to read worker results. Change test identity to exercise
candidate isolation and recruiter conversations. Demo records survive restarts.

**Simulated AI is the default:** no Groq/Gemini network calls or provider keys are
needed. PDFs, persistence, queues, local embeddings and Qdrant remain real. Simulated
CV extraction recognizes `Field: value` lines (as in the sample PDF); other CVs can
be corrected manually. These deterministic responses test workflows, not AI quality.

For actual provider responses, configure keys and positive account RPM/TPM/RPD
limits in `.env`, stop the demo API, and restart it with:

```powershell
$env:SID_DEMO_LIVE_AI='1'
uv run python -m app.demo
```

Live mode retains the configured account scope and quotas, sends entered source
text to the configured providers, and is visibly marked in the console. Remove the
variable to return to simulated mode. Candidate offer search uses synthetic fixtures;
real private matching, recruiter matching and submission remain disabled.
Approval still records consent only. All confirmation/version/ownership checks
remain active. Optional UI regression checks run with `uv run --group demo python
-m pytest tests/test_demo.py -q`; MongoDB checks require `SID_TEST_MONGODB_URI`.

With the simulated demo running, exercise all three facade contracts with
`uv run python -m examples.agent_consumer`. Export versioned OpenAPI and the
proposed profile-sync schema with `uv run python -m scripts.export_agent_contract`.

## Setup

```powershell
uv sync
```

Copy `.env.example` to `.env` if it does not exist, then fill `SID_SERVICE_API_KEY`,
`GROQ_API_KEY` and `GEMINI_API_KEY` locally. The service key is a secret shared with
your trusted backend, not a key to put in browser JavaScript.

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

The requested Groq default is `llama-3.1-8b-instant`. Groq's current catalog labels
it Enterprise; verify your account can access it. Set `GROQ_MODEL` explicitly if
you choose a different model. The Gemini default is `gemini-3.1-flash-lite`.
Neither API integration guarantees free-tier eligibility or unlimited usage.

## Run locally

```powershell
uv run fastapi dev
```

The development server runs at http://127.0.0.1:8000 with automatic reload.

- Interactive API documentation: http://127.0.0.1:8000/docs
- Alternative documentation: http://127.0.0.1:8000/redoc
- Health check: http://127.0.0.1:8000/health
- Configuration readiness: http://127.0.0.1:8000/health/ready

The app starts without provider keys. Preview generation requires a service key
and at least one provider key. MongoDB-backed generation also requires explicit
account quota limits. Readiness makes no AI calls; with MongoDB enabled, it checks
database reachability, platform identity integration and provider-limit configuration.

## Generate a draft preview

In `/docs`, choose **Authorize**, enter your local `SID_SERVICE_API_KEY`, then try
`POST /api/v1/application-drafts/preview` with `examples/draft-request.json`.

Or call from a trusted backend / local PowerShell:

```powershell
$requestHeaders = @{ "X-Service-Key" = "replace-with-your-local-service-key" }
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/application-drafts/preview `
  -Headers $requestHeaders -ContentType "application/json" `
  -InFile examples/draft-request.json
```

The response includes `cover_letter`, `provider`, `model`, `fallback_used`,
`status: "draft"`, `requires_human_approval: true`, and `persisted: false`.
This preview endpoint does not save or submit applications. Content accuracy still
needs human review. Persistent drafts use the separate Phase 2 workflow below.

`GET /api/v1/ai/status` uses the same service key and exposes circuit states, never
credentials. Timeouts, transient errors and quota errors can fall back to Gemini.
Content refusals and ordinary invalid requests are not retried across providers.
Both providers unavailable returns 503, preserving the caller's input for retry.

Without MongoDB, breakers are process-local. With `MONGODB_URI`, all API/worker
instances use shared MongoDB circuit states, recovery leases and quota reservations.
Use identical account scopes and budget settings across processes sharing credentials.

## Phase 2: persistent workspace

Start the local development replica set:

```powershell
docker compose -p sid-phase2 -f compose.mongodb.yml up -d --wait
```

Set MongoDB connection/database and actual provider RPM/TPM/RPD limits in `.env`.
Zero limits disable that provider in Phase 2. The supplied schema is mapped through
`app/platform.py`; confirm collection names and string-ID fields with the main platform.

Connect the existing backend's trusted login verifier using
`create_app(verify_user_id=...)`. The default application leaves persistent user
endpoints disabled until that verifier is connected. A service key is not a user login.
Roles and account status are reloaded from the platform's `User` collection.

The workflow saves a profile, accepts its exact version, queues generation with an
idempotency key, then permits letter edits and explicit candidate approval. It keeps
immutable history and invalidates approval when sources or text change.

Start the worker in a separate terminal after platform records, keys and limits are ready:

```powershell
uv run python -m app.worker
```

[Phase 2 documentation](docs/phase-2.md) contains endpoint bodies, authentication
contract, schema mapping, lease behavior, policy-update command and remaining
integration work. Submission and platform `Application` write-back belong to phase 5.

## Tests

```powershell
uv run python -m pytest -q
uv run ruff check app tests
uv run ruff format --check app tests
```

These commands run offline tests; MongoDB integration tests are skipped without
an explicit test URI. To verify Phase 2 against the local replica set:

```powershell
$env:SID_TEST_MONGODB_URI='mongodb://127.0.0.1:27018/?replicaSet=rs0&directConnection=true'
uv run python -m pytest -q
```

Tests use synthetic records, fake keys and mock provider responses. Each integration
test creates and removes its own randomly named test database. No live model is called.
Tests do not establish compatibility with the unconnected platform's authentication,
live model quality or account eligibility.

## Plans and reference data

- [AI scope audit and API delivery roadmap, 12 October 2026](docs/ai-scope-and-api-roadmap-2026-10-12.md) - current priorities against the supplied PDFs and pipeline.
- [Agent v1 facade, synthetic offer search and backend consumer](docs/agent-api.md) - A/B delivery and pending integration gates.
- [Implementation phases and test gates](docs/implementation-phases.md)
- [Phase 2 MongoDB integration and setup](docs/phase-2.md)
- [MVP architecture and functional plan](docs/plan-mvp-ia.md)
- [Knowledge-base sources and availability](docs/knowledge-base-sources.md)
- [Application dossiers, evidence and approval](docs/phase-5.md)
- [Containers, operational status and CI](docs/operations.md)
- [Hardening follow-up review, 9 October 2026](docs/review-2026-10-09.md)
- [Next system improvement plan](docs/improvement-plan-2026-10-09.md)
- [Offline AI evaluation and evidence corrections](docs/ai-evaluation.md)
- [Platform integration contract inputs](docs/platform-integration-contract.md)
- [Phase 6 implementation review and remaining gates](docs/phase-6.md)

Application lifecycle and health routes are in `app/main.py`, draft routes in
`app/api/drafts.py`, and provider routing in `app/ai/`. Dependencies are declared
in `pyproject.toml` and locked in `uv.lock`.

For a server without development reload, run `uv run fastapi run`.
