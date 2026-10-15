# Running and verifying the backend

The API, draft worker, CV worker and reference index worker run separately.
MongoDB must be a replica set; Qdrant holds rebuildable public reference vectors.
Use the Phase 2/3 documents for database and index setup. The database Compose
files are loopback-only development services, not production database deployments.

## Runtime image

```powershell
docker build -t sid-agent:local .
docker run --rm --read-only --tmpfs /tmp:rw,noexec,nosuid,size=128m -p 127.0.0.1:8000:8000 --env-file .env sid-agent:local
```

The image runs as UID/GID 10001 with locked runtime dependencies. The build context
excludes credentials, PDFs, tests, knowledge snapshots and model files. When Qdrant
is configured, mount the checksum-verified model directory read-only and set
`EMBEDDING_MODEL_DIRECTORY` to its container path. Container database URLs must
point to reachable services; host `127.0.0.1` URLs cannot be reused inside it.

The default API leaves user routes disabled until a trusted login verifier is
provided via `create_app(verify_user_id=...)`. Once the contract is available, add
the platform factory and use its module as the Uvicorn target. A shared service
key does not substitute for user authentication.

Override the command for workers; disable the image's API-only healthcheck:

```powershell
docker run --rm --no-healthcheck --env-file .env sid-agent:local python -m app.worker
docker run --rm --no-healthcheck --env-file .env sid-agent:local python -m app.cv.worker
```

The index worker uses `python -m app.knowledge.cli worker` and needs its model
mount, MongoDB and Qdrant configuration. Processes sharing provider keys must use
the same database, account scope and provider budget policy. Scale processes after
measuring demand: extra workers do not increase provider quotas. Embedding memory
and database load also constrain concurrency. Capacity is not yet load-tested.

## Request and queue admission

The ASGI boundary buffers at most `HTTP_JSON_MAX_BYTES` (256 KiB by default) for
ordinary requests and 5 MiB for `POST /api/v1/cv-imports`. It checks declared
lengths before reading and also counts actual chunks, including unknown lengths.
The complete body must arrive within `HTTP_BODY_TIMEOUT_SECONDS` (default 10).
Oversized bodies return 413, an expired read returns 408, and exhausted buffering
capacity returns 503. `HTTP_MAX_CONCURRENT_BODIES` (default 16) bounds requests
from body reading through response completion, so buffered bodies remain bounded
while generation runs. These limits apply before authentication and field validation.
GET liveness/readiness handlers consume no body and bypass buffering admission,
so capacity exhaustion does not cause a healthy process to fail its health probe.
Set ingress connection, header, request-rate and body limits too; ASGI cannot bound
bytes the HTTP server or proxy has already buffered. Limits multiply by API processes.

New CV imports and letter/dossier jobs share MongoDB admission limits per user ID:
5 pending/running jobs, 50 MiB of stored PDF bytes and 10 accepted new requests per
UTC calendar minute by default. The global pending/running ceiling across both
queues is 1000. Configure the `ADMISSION_*` settings identically on every API
instance sharing this database. Existing work and files count when upgrading;
no historical counter migration is required. Startup initializes the admission
guard and indexes via `ensure_indexes` before accepting persistent requests.

Admission and enqueue happen in one transaction, serialized through a shared
guard. Failed transactions consume no capacity or rate allowance. Identical
idempotent replays consume no new capacity or rate allowance. Terminal jobs
release queue capacity; retries and expired running leases retain it. Failed,
reviewed and accepted imports retain storage capacity until explicit PDF deletion.
Deleting a CV releases its bytes and unfinished job in the deletion transaction.
Per-user caps return 429 and the global queue ceiling returns 503 with normalized
error codes. These controls limit new CV/draft work; profile/history/outbox retention
and request-rate protection for other endpoints remain separate work.

The shared guard prioritizes strict bounds over admission throughput. Load-test
contention before raising the ceiling or adding many API instances. Caps bound one
user's backlog; tenant scheduling/fairness still needs the organization contract.

## CV worker failures and supervision

Expected parser launch, I/O and monitor resource failures produce
`cv_parser_unavailable`, retry after 5 seconds and stop after
`WORKER_MAX_ATTEMPTS`. No CV text goes to a provider when parsing fails. Invalid
PDFs, parser deadlines and content limits remain permanent task failures.
Unexpected programming errors still terminate the worker and preserve its lease
for recovery; they are not silently converted into successful work.

Run each worker under a supervisor that restarts on nonzero exit, records stderr
and alerts on repeated restarts. For the Docker commands above, use detached
containers with `--restart=on-failure:5` in place of `--rm`. A replacement worker
reclaims expired leases; terminal task failures remain visible in queue status.
Restart exhaustion requires operator action. Queue counts alone do not establish
worker health; heartbeat monitoring and recovery load tests are still required.

## Status and recovery

- `/health` is process liveness; the image healthcheck uses it.
- `/health/ready` checks configuration and, with MongoDB, database access, identity
  integration and shared provider limits. It does not test actual provider access,
  model quality or retrieval readiness.
- `GET /api/v1/operations/status` requires `X-Service-Key` and MongoDB. It returns
  queue counts, expired lease counts, provider circuit status and integration
  flags without source text or identities. These are sequential snapshots, not
  transactionally consistent metrics or proof that a worker is alive. Aggregations
  have a server time limit.
- `GET /api/v1/ai/status` reports provider state without keys or prompts.
- `python -m app.knowledge.cli status` reports reference/index generation state.

Expired leases are reclaimed by running workers; late workers cannot commit under
an old token. Final failures remain visible for investigation. Use the documented
index retry/rebuild commands after correcting failures; do not reset approval
records manually. The outbox currently has no sender, so pending events are expected.

Back up MongoDB, including source versions, approvals and policy state. Keep
Qdrant's embedding fingerprint, source snapshots and generation metadata together.
Phase 3 tests verify vector snapshot restore; a production database restore drill
and agreed recovery objectives remain required. A vector backup does not protect
profiles or approvals.

## Conversation deletion

`DELETE /api/v1/conversations/{id}` requires the owning user and returns `204`.
It deletes the head, keyword memory and turns in one transaction. Late generation
cannot recreate them. Unknown/other-user IDs and repeat deletion return `404`.
Deletion does not remove the linked profile or copies processed by providers.
Account-wide retention, backups and provider data policy still need decisions.

## Verification and release gates

Local verification on 9 October 2026 after Phase 6: **241 tests passed** with real disposable
MongoDB/Qdrant and the local embedding model; lint and formatting passed. The
runtime image built and passed liveness as UID 10001 on a read-only filesystem.
Oversized unauthenticated JSON returned 413 in the runtime image. Fresh raw-file
checkout hashes were verified on Windows and Linux with both line-ending settings.
See the [follow-up review](review-2026-10-09.md) and
[new improvement plan](improvement-plan-2026-10-09.md).
The [Phase 6 review](phase-6.md) covers the added evidence-correction and offline
evaluation paths. Synthetic evaluation output does not establish live AI quality.
One upstream Starlette/httpx deprecation warning remains. Generation responses
were synthetic; no live provider was called.

`.github/workflows/tests.yml` runs lint, formatting, unit tests, real MongoDB/Qdrant
integration tests, the pinned embedding smoke test, an image build and container
liveness check. It uses disposable databases and synthetic generation responses
without provider keys. Model download requires network access. Action commits and
dependency versions are pinned; container base tags need periodic maintenance.

```powershell
$env:SID_TEST_MONGODB_URI='mongodb://127.0.0.1:27018/?replicaSet=rs0&directConnection=true'
$env:SID_TEST_QDRANT_URL='http://127.0.0.1:6335'
$env:SID_TEST_EMBEDDING_DIRECTORY='.models/multilingual-minilm'
uv run python -m pytest -q
uv run ruff check app tests
uv run ruff format --check app tests
```

Before a live pilot: integrate platform identity/UI, verify actual model access and
account budgets, human-score the evaluation corpus, test load/recovery, and resolve
retention/submission contracts. Matching stays disabled until offer visibility and
candidate discovery are confirmed. Passing tests does not establish live model
quality or successful end-to-end platform submission.
