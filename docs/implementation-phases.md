# Implementation phases and test plan

Updated: 4 October 2026. MongoDB is the existing self-hosted source of truth;
Qdrant is the independent reference search index. Start with a small deployment and
measure demand before increasing capacity.

## Delivery sequence

| Phase | Implementation | Required tests / exit condition |
|---|---|---|
| 1 — AI foundation (implemented) | Configuration, internal service authentication, Groq primary, Gemini fallback, per-provider circuit breakers, bounded generation and unsaved cover-letter preview | Adapter contracts, fallback, quota cooldowns, recovery probes, concurrency, cancellation, protected endpoints, draft-only behavior, controlled failure responses. Offline tests pass; live account access remains a separate check. |
| 2 — Persistence core implemented; live integration pending | Supplied UML mapped read-only; candidate/company ownership; versioned profiles/drafts, approvals, atomic outbox, leased tasks/worker, shared provider circuits and RPM/TPM/RPD budgets | Real replica-set integration tests pass for isolation, atomic rollback, stale versions, duplicate requests, lease recovery, account status and shared provider limits. Existing login verifier, actual collection names and main database connection still require integration. See [Phase 2 delivery](phase-2.md). |
| 3 — Reference ingestion/search implemented | 51,921 normalized ESCO/ROME/O*NET/GeoNames references with IDs/licenses; pinned 384-dimension local multilingual ONNX model; MongoDB canonical records and batched/fenced Qdrant jobs; protected reference search | Real MongoDB/Qdrant tests for idempotent imports, encoding/coverage, aliases, dimensions, updates/deletions/revocation, duplicates/races, parallel workers, cutover, rebuild and native snapshot restore; real multilingual model smoke test. ESCO release/domain review and main-platform integration remain pending. See [Phase 3 delivery](phase-3.md). |
| 4 — CV import and profile evaluation implemented; live quality pending | Private bounded PDF upload, isolated parser, shared Groq/Gemini gateway, page-grounded facts, immutable review drafts, candidate corrections/confirmation, deterministic completeness | Synthetic French/English PDF corpus; malformed/encrypted/scanned files, limits, missing/unsupported facts, conflicts/dates, stale versions, atomic history, concurrent acceptance, ownership/deletion and account status. No overwrite or automatic profile validation. Human-scored live extraction and main-platform integration remain pilot gates. See [Phase 4 delivery](phase-4.md). |
| 5 — Persistent application dossier and human approval | Link validated profile, offer and company versions; evidence matrix, editable letter, CV suggestions; approve exact version and destination; integrate submission | Unsupported-claim evaluation, missing company context, user edits, approval invalidation, ownership, concurrent submit, retry after ambiguous delivery, exactly-once business effect where destination supports idempotency. No send without server-side approval. |
| 6 — Conversation foundation implemented; matching explicitly disabled | Diagram-aligned context routing, private history, validated profile context, top-five reference RAG, cited advice and keyword memory. Job/candidate indexes, strict filters and matching remain pending. See [pipeline review](pipeline-alignment.md). | Conversation ownership, idempotency, lease fencing, atomic history/memory, stale profiles, citation validation and failure recovery. Future matching gates: strict Sfax/PFE constraints, visibility/discovery, junior projects, explanation fidelity, anonymization and human-labelled top-five relevance. |
| 7 — Pilot and production readiness | End-to-end UI integration, operational metrics, tenant limits, backups, provider data-handling review, deployment and capacity tuning | Representative load at baseline/5x/10x, p95 latency, queue age, provider throttling, MongoDB/Qdrant/worker failure and recovery, deletion completeness, restore drill, cost/token budget. Human approval and access-control failures block release. |

The platform UML has been supplied and mapped. MongoDB credentials, actual collection
names/topology and the platform's identity contract are still pending. These are
required to enable Phase 2 on the real platform; the core is verified locally.
The preview service key authenticates a trusted backend caller; it is not a
replacement for end-user authentication or organization authorization.

## Phase 1 implemented path

```text
Trusted backend → authenticated FastAPI preview endpoint
    → request validation + process concurrency limit
    → Groq circuit → Groq / Llama 3.1
    → eligible provider failure → Gemini circuit → Gemini
    → unsaved draft + provider/model provenance → human review
```

`POST /api/v1/application-drafts/preview` accepts candidate text, job text,
optional company context and `fr`/`en`. It returns a draft with
`requires_human_approval=true` and `persisted=false`. There is no publication,
approval or submission endpoint in phase 1. Phase 2 supplies a separate persistent
draft/approval workflow; submission is phase 5.

The prompt separates source data from instructions and prohibits invented facts.
These instructions are not a factual-accuracy guarantee: human review and later
evidence validation remain necessary. Current tests prove routing and access
behavior, not the quality of a live model's letters.

## Provider choices and account verification

- **Primary:** Groq, configured as `llama-3.1-8b-instant` to preserve the requested
  Llama 3.1 choice. On the check date, Groq's model catalog marks this model
  **Enterprise / Contact Sales**. Its availability on a free account must be checked;
  neither the implementation nor documentation promises free access.
- **Fallback:** Gemini, default `gemini-3.1-flash-lite`, configurable through
  `GEMINI_MODEL`. Google's pricing page lists a free tier; actual access and quota
  depend on the user's account/project. A free tier is not a capacity guarantee.
- Missing keys disable that provider. If only Gemini is configured, it can serve
  previews and is explicitly reported as fallback. If neither is configured, the
  application remains live but is not ready for generation.
- There are no live calls in tests and no automatic model changes. Add credentials
  locally in `.env`; never in chat, source control or browser JavaScript. Check
  account limits and model access before enabling real traffic.
- This layer handles **text generation only**. Embeddings require a separate model
  choice. Never fall back between incompatible embedding models for an existing
  Qdrant collection; query and indexed vectors must share model/version/dimensions.

References: [Groq models](https://console.groq.com/docs/models),
[Groq quota headers](https://console.groq.com/docs/rate-limits),
[Groq chat API](https://console.groq.com/docs/api-reference),
[Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing),
[Gemini rate limits](https://ai.google.dev/gemini-api/docs/rate-limits),
[Gemini REST contract](https://ai.google.dev/api/generate-content).

## Circuit-breaker behavior

Each provider has an independent circuit, shared by requests within one API
process. States use a monotonic clock:

1. **Closed:** try the provider. Three consecutive transient/invalid-response
   failures open its circuit by default. A successful response resets the count.
2. **Open:** skip the provider and try the next configured provider. Default
   recovery delay is 30 seconds. This request does not sleep or retry the failed
   provider, preserving quota and bounding latency.
3. **Half-open:** after cooldown, allow one recovery probe. Other concurrent
   requests skip to fallback. Success closes the circuit; failure reopens it.
   Cancellation releases the probe, and results from an older circuit generation
   cannot close or reopen a newer generation.

| Condition | Action |
|---|---|
| Connection failure, per-provider timeout, HTTP 408/425/5xx | Count failure; try fallback |
| HTTP 429 | Open immediately; honor usable Retry-After / Gemini RetryInfo with at least the normal cooldown; try fallback |
| HTTP 401/403/404 or recognized invalid-key/decommissioned-model error | Open immediately for the configuration cooldown (300s default); try fallback; log only normalized error category |
| Empty/malformed or truncated generation | Count failure; try fallback; never return it as a completed draft |
| Recognized content refusal | Return `422 generation_blocked`; no cross-provider retry |
| Other rejected request (e.g. HTTP 400) | Return `502 provider_request_rejected`; no fallback or outage count |
| Both providers failed, disabled or open | Return `503 ai_unavailable` with Retry-After |
| Process concurrency limit reached | Return `503 ai_busy`; do not queue or call providers |

Default bounds: four simultaneous preview requests per process, 15 seconds per
provider attempt, 35 seconds overall, and 1,200 output tokens. Provider cooldown
hints are bounded to 24 hours; malformed hints use the configured recovery delay.
Each request makes at most one attempt per provider. These are engineering defaults,
not a claim about the free-tier request/token allowance.

`GET /api/v1/ai/status` is service-authenticated and reports provider configuration
and circuit states without keys or prompts. `/health` is liveness;
`/health/ready` checks local key configuration only and makes no provider calls.

The Phase 1 breaker and concurrency limiter are **process-local** without MongoDB.
With MongoDB configured, Phase 2 uses shared cooldowns, probe/concurrency leases and
account-wide request/token budgets. Every API/worker sharing credentials must use
the same account scope, database and configured policy. Live platform integration,
real account limits and load validation remain required before production scaling.
A circuit breaker and a quota limiter are separate controls.

## Test execution

```powershell
uv sync
uv run pytest -q
uv run ruff check app tests
uv run ruff format --check app tests
```

Phase 1 tests use HTTPX mock transports, fake providers, fake clocks and real
FastAPI routing/lifespan. Phase 2 tests use a real disposable replica set when
`SID_TEST_MONGODB_URI` is set, as documented in [Phase 2 setup](phase-2.md).
No live generation API is contacted. Phase 3 Qdrant/model tests are explicitly
opt-in as documented in [Phase 3 setup](phase-3.md).

The provider contract suite covers request headers/bodies and response parsing;
the breaker suite covers state transitions, cooldowns and stale concurrent results;
the gateway suite covers fallback, deadlines, cancellation and admission limits;
API tests cover authentication, validation, errors and draft-only behavior.

Later CI should add disposable MongoDB replica-set and Qdrant services for
integration tests. Keep model-quality evaluation separate from deterministic unit
tests: use a versioned synthetic/consented corpus and human scoring, not exact
string comparisons. Live provider smoke tests must be opt-in, use synthetic data,
and record model access, latency and quota metadata without secrets.

## Completion criteria for this iteration

- Phase plan and required test gates recorded.
- Both REST adapters wired into a reusable gateway with explicit failover rules.
- Protected, bounded, draft-only endpoint runnable without database setup.
- Circuit/fallback/authentication tests and lint/format checks pass.
- `.env.example`, sample request and run instructions available.

Live generation-model access, the main platform login/database connection,
live extraction-quality evaluation and submission remain pending. Durable drafts, profile versions
and approvals are delivered by Phase 2; public reference ingestion, local
embeddings and Qdrant indexing/search are delivered by Phase 3. Private PDF import,
candidate review/confirmation and profile completeness are delivered by Phase 4.
