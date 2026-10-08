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
Application dossiers add quoted evidence, CV suggestions and source-bound approval
using candidate-supplied job/company text. See [Phase 5](docs/phase-5.md).
Conversation deletion, queue status, a non-root Docker image and integration CI
provide the [operations baseline](docs/operations.md). Platform login, canonical
offer binding and submission still require the main backend's contracts.

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

- [Implementation phases and test gates](docs/implementation-phases.md)
- [Phase 2 MongoDB integration and setup](docs/phase-2.md)
- [MVP architecture and functional plan](docs/plan-mvp-ia.md)
- [Knowledge-base sources and availability](docs/knowledge-base-sources.md)
- [Application dossiers, evidence and approval](docs/phase-5.md)
- [Containers, operational status and CI](docs/operations.md)

Application lifecycle and health routes are in `app/main.py`, draft routes in
`app/api/drafts.py`, and provider routing in `app/ai/`. Dependencies are declared
in `pyproject.toml` and locked in `uv.lock`.

For a server without development reload, run `uv run fastapi run`.
