# SID Agent API

A recruitment AI backend using Python 3.11+, FastAPI and uv. The first implemented
slice generates an unsaved cover-letter draft using Groq, with Gemini fallback and
an independent circuit breaker for each provider.

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

The app starts without provider keys. Generation remains unavailable until a
service key and at least one provider key are configured. Readiness checks only
local configuration, not account validity, and consumes no provider quota.

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
This endpoint does not save or submit applications. Content accuracy still needs
human review. Full user authorization, persistent drafts and approval/submission
workflows come in later phases.

`GET /api/v1/ai/status` uses the same service key and exposes circuit states, never
credentials. Timeouts, transient errors and quota errors can fall back to Gemini.
Content refusals and ordinary invalid requests are not retried across providers.
Both providers unavailable returns 503, preserving the caller's input for retry.

The first breaker is process-local. Coordinate provider quotas and cooldowns before
running multiple production replicas; see the implementation plan below.

## Tests

```powershell
uv run pytest -q
uv run ruff check app tests
uv run ruff format --check app tests
```

Tests are offline and use fake keys and mock HTTP responses. No database or live
model is required. They do not establish live model quality or account eligibility.

## Plans and reference data

- [Implementation phases and test gates](docs/implementation-phases.md)
- [MVP architecture and functional plan](docs/plan-mvp-ia.md)
- [Knowledge-base sources and availability](docs/knowledge-base-sources.md)

Application lifecycle and health routes are in `app/main.py`, draft routes in
`app/api/drafts.py`, and provider routing in `app/ai/`. Dependencies are declared
in `pyproject.toml` and locked in `uv.lock`.

For a server without development reload, run `uv run fastapi run`.
