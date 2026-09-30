# Phase 2: MongoDB persistence and platform integration

## Delivery status

The persistence, versioning, approval/outbox transaction, leased generation worker,
shared circuit breakers and account-budget controls are implemented and exercised
against a real MongoDB 8.0 replica set with synthetic records and mocked AI APIs.

Verification on 30 September 2026: **103 tests passed** (66 offline and 37 MongoDB
integration tests), with lint and formatting checks passing.

**Live platform integration is pending.** The supplied UML establishes field names
and ownership, but the existing authentication contract, actual collection names,
MongoDB connection and deployment topology have not been provided. User endpoints
fail closed until the platform's login verifier is connected. Tests do not prove
compatibility with an unseen platform implementation or live model access.

## Mapping the supplied schema

The source is the [supplied UML diagram](../WhatsApp%20Image%202026-09-24%20at%2016.35.45.jpeg).

| Platform class | Read-only mapping | Ownership rule |
|---|---|---|
| `User` | `id`, `role`, `isActive` | Look up an active user after login verification; roles come from MongoDB |
| `CandidateProfile` | `id`, `userId` | A candidate can link only their own source profile |
| `CompanyProfile` | `id`, `userId` | Recruiter's company is resolved server-side |
| `JobOffer` | `id`, `companyId` | Recruiter can read an offer only for their resolved company |
| `Application` | Existing candidate/job links and `isValidatedByCandidate` | Submission/write-back belongs to phase 5 |

Collection names are configurable: `PLATFORM_USERS_COLLECTION`,
`PLATFORM_CANDIDATES_COLLECTION`, `PLATFORM_COMPANIES_COLLECTION`,
`PLATFORM_OFFERS_COLLECTION`. Defaults are naming assumptions that must be confirmed.
The reader expects the UML's string `id` field, uppercase role enum, camel-case
relationships, and a boolean `isActive`. If the actual documents use `_id`, ObjectId,
different names or embedded records, adapt `app/platform.py` before connecting them.

Candidates have a private `candidate:<User.id>` scope. The UML does not assign them
to a company. Recruiter scope comes from `CompanyProfile.id`; callers cannot supply
the acting user, role or scope in request bodies. Candidate workspace endpoints
allow the candidate owner only; recruiter/admin access is denied. Recruiter search
visibility and administrative access policy are later features.

The AI service stores its own reviewable snapshots and workflow records in `sid_`
collections, in the configured MongoDB database. It does not create replacement
users or modify the platform's candidate, company, offer or application records.
Approved profile write-back and offer/company version binding remain phase 4/5 work.

## Connecting existing authentication

`create_app(..., verify_user_id=...)` accepts an asynchronous verifier with this
contract: `async verify_user_id(request: Request) -> str`.

It must validate the existing JWT/session using the platform's real rules and return
the authenticated `User.id`, or raise an authentication error. Do not use client-
supplied user-ID headers or unverified token claims. FastAPI then reloads `User`,
checks `isActive` and resolves the role/company using the reader. Disabling an
account is therefore checked on every subsequent user request. The worker checks
account status before generation and again before saving its result.

The default application has **no verifier**. Persistent user endpoints return
`503 platform_identity_not_integrated`; `X-Service-Key` grants no candidate identity.
Phase 1 preview/status endpoints still use the internal service key. Test-only
identity fixtures are confined to `tests/`; they are never enabled by settings.

## Storage and invariants

| Collection | Responsibility |
|---|---|
| `sid_profiles` | Current profile snapshot, version and accepted version |
| `sid_profile_versions` | Immutable profile text/history, optional platform profile reference |
| `sid_drafts` | Current draft, pinned profile version, frozen job/company inputs and approval pointer |
| `sid_draft_versions` | Immutable letter revisions and provenance |
| `sid_approvals` | Candidate approval, exact draft/profile versions, content/destination hash, active/invalidation history |
| `sid_tasks` | Idempotent generation requests, retries, leases and sanitized outcomes |
| `sid_outbox` | Durable domain events committed with the corresponding changes |
| `sid_provider_state` | Shared account/provider circuit, probe/concurrency leases and quota buckets |

- `expected_version` is mandatory for edits and confirmations. Stale requests return
  409, preserving history rather than overwriting another edit.
- A profile must be explicitly accepted before generation. Editing it resets acceptance,
  invalidates dependent approvals and makes old-source drafts ineligible for approval.
- Letter edits create a new revision and invalidate approval. A profile edit during
  generation discards the stale result.
- Approval requires an actual JSON `true`, an exact draft version and a destination
  identifier. The server records a hash of the letter, sources and destination.
  Changing destination requires a new approval; the old one is retained as invalidated.
- Approval and its outbox event commit together in a snapshot/majority transaction.
  Duplicate concurrent approvals reuse the existing approval for the same version
  and destination. A profile write guard prevents concurrent profile editing from
  leaving an active approval based on stale data.
- `Idempotency-Key` is required for queued generation. It is scoped to the candidate;
  reusing it for different input returns 409. Task/draft creation is atomic.

The platform `Application` is never submitted by this phase. A `draft.approved`
outbox event is an audit/integration event, not permission for an unspecified
consumer to publish. Phase 5 must verify the current approval, exact sources and
destination again before connecting any submission handler.

## API workflow

| Operation | Endpoint |
|---|---|
| Save a profile snapshot | `POST /api/v1/profiles` |
| Read / edit a profile | `GET` / `PUT /api/v1/profiles/{id}` |
| Accept an exact profile version | `POST /api/v1/profiles/{id}/accept` |
| Read an immutable profile revision | `GET /api/v1/profiles/{id}/versions/{version}` |
| Queue a persistent draft | `POST /api/v1/application-drafts` with `Idempotency-Key` |
| Poll generation | `GET /api/v1/tasks/{id}` |
| Read / edit a letter | `GET` / `PUT /api/v1/application-drafts/{id}` |
| Read an immutable letter revision | `GET /api/v1/application-drafts/{id}/versions/{version}` |
| Record candidate approval | `POST /api/v1/application-drafts/{id}/approvals` |

Profile snapshot input:

```json
{"content":"Candidate-reviewed profile text, at least 20 characters.","external_profile_id":"optional-owned-CandidateProfile.id"}
```

Acceptance input: `{"expected_version":1,"confirmed":true}`.

Queued draft input:

```json
{"profile_id":"saved-SID-profile-id","profile_version":1,"job_description":"Offer text, at least 20 characters.","company_context":"Optional company context.","language":"fr"}
```

Letter edit input:
`{"expected_version":1,"cover_letter":"Candidate-edited letter, at least 20 characters."}`.

Approval input:
`{"expected_version":2,"confirmed":true,"destination_id":"intended-JobOffer.id"}`.
This phase records the destination as an opaque identifier; existence, openness,
offer/company snapshots and submission eligibility are validated in phase 5.

## Workers and outbox

Run workers separately from the API. Claiming is atomic; every claim gets a unique
lease token. Heartbeats extend a valid lease. Expired work can be reclaimed and a
stale worker cannot acknowledge it or persist a result. Completion commits task
status, draft revision and generated-event outbox entry together.

Transient generation failure reschedules the task using Retry-After, with bounded
attempts. Refusal, rejected input, stale profile or inactive account ends the task
with a sanitized error code. A crashed final attempt is moved to failed on recovery,
without another provider request. Cancellation leaves leased work recoverable.

Delivery is **at least once**. A crash after the provider responds but before the
transaction commits can cause another AI call; fencing prevents duplicate saved
results, but does not eliminate duplicate provider cost. Durable outbox events use
the same lease primitives; actual search/submission consumers come in later phases.
Consumers must be idempotent and should acknowledge only after a durable effect.

## Shared provider controls

Setting `MONGODB_URI` switches both API previews and workers to MongoDB-backed
breakers. They share account/provider state keyed by `AI_ACCOUNT_SCOPE` plus provider.
Every process using the same provider credentials must use the same scope, database
and limits. Requests made by other applications outside this gateway are not counted.

Set positive account limits for RPM, TPM and RPD for each enabled provider. Defaults
are zero, disabling calls until real account quotas are configured. Quota admission
and provider lease acquisition happen atomically before an HTTP attempt. Inconsistent
limits across processes fail closed, rather than multiplying allowances.

The request token reservation uses UTF-8 input bytes + configured maximum output
tokens + 256 framing tokens. It is a conservative estimate, not live provider usage
metering. Reservations are not refunded after cancellation or uncertain failures.
Oversized reservations are rejected before calling a provider.

Minute budgets use one-second buckets that retain reservations for 60–61 seconds.
Daily budgets use hourly buckets retained for 24–25 hours. These conservative
rolling windows prevent boundary bursts while keeping state bounded to roughly
61 minute buckets and 25 day buckets per account/provider. Daily capacity can
reopen up to an hour later than an exact 24-hour limiter. Provider 429 responses
remain authoritative and trigger the shared cooldown.

Three failures open the circuit by default; quota/configuration failures open it
immediately. One leased recovery probe is allowed account-wide. Crashed probes expire,
and stale results cannot close a newer circuit generation. Clock values come from
MongoDB, normalized to UTC. A MongoDB outage fails closed; the service does not
quietly bypass shared limits with a process-local fallback.

To tune existing account limits, stop account traffic, wait for active leases to
finish, update the environment consistently, then apply the policy explicitly:

```powershell
uv run python -m app.db.provider_policy groq
uv run python -m app.db.provider_policy gemini
```

This preserves charged reservations and cooldowns. It refuses changes during
active leases. Do not delete provider-state documents to gain fresh quota.

## Local setup and verification

Use a dedicated local replica set, independent of the main platform database:

```powershell
docker compose -p sid-phase2 -f compose.mongodb.yml up -d --wait
uv sync
```

The development MongoDB listens on loopback port 27018. It has no authentication and
is intended for local development only. For the real platform, use its authenticated
MongoDB URI and confirm replica-set/sharded-cluster support. The service rejects a
standalone MongoDB server because its cross-document invariants need transactions.

Set `MONGODB_URI` and `MONGODB_DATABASE` locally. The development URI is
`mongodb://127.0.0.1:27018/?replicaSet=rs0&directConnection=true`.
Startup creates only SID-owned collections/indexes; the database user needs their
read/write/index permissions and read access to the mapped platform collections.
An explicit idempotent initializer is also available:

```powershell
uv run python -m app.db.init
uv run fastapi dev
# Separate terminal, after keys, budgets and platform records are configured:
uv run python -m app.worker
```

User routes still require the real login verifier. `/health/ready` checks MongoDB,
identity integration and provider-limit configuration. It makes no AI calls and
does not certify live model access or currently available quota.

Run the complete test suite:

```powershell
$env:SID_TEST_MONGODB_URI='mongodb://127.0.0.1:27018/?replicaSet=rs0&directConnection=true'
uv run pytest -q
uv run ruff check app tests
uv run ruff format --check app tests
```

Integration tests create randomly named `sid_test_<uuid>` databases and remove only
those databases. They never read the application's MongoDB URI, use real provider
keys, or contact external AI services. Without `SID_TEST_MONGODB_URI`, the MongoDB
tests are explicitly skipped; that is not full Phase 2 verification.

For local shutdown: `docker compose -p sid-phase2 -f compose.mongodb.yml stop`.

References: [PyMongo transactions](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/crud/transactions/).
