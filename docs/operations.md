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

Local verification on 8 October 2026: **200 tests passed** with real disposable
MongoDB/Qdrant and the local embedding model; lint and formatting passed. The
runtime image built and passed liveness as UID 10001 on a read-only filesystem.
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
