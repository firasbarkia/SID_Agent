# Phase 4: private CV import and profile evaluation

Implemented 2 October 2026. This is a backend slice; the main platform's login
adapter and frontend remain integration work. Synthetic fixtures are used at the
user's request. Live Groq/Gemini extraction quality is not yet measured.

## Workflow

```text
Authenticated candidate → bounded private PDF upload → MongoDB file/import/task
    → separately scalable CV worker → isolated text parser → page text
    → shared Groq circuit/quota → eligible failure → Gemini circuit/quota
    → validated JSON + checked page quotes → versioned review draft
    → candidate corrections → explicit exact-version confirmation
    → new validated profile + immutable snapshot + atomic outbox event
```

The import never replaces an existing profile, writes the main platform's
CandidateProfile, publishes an application or adds CVs to Qdrant/public reference
data. Confirmation creates a new SID profile using the existing profile lifecycle.
The candidate can use that profile for Phase 2 application drafts. Application
submission is Phase 5.

## Run

Use the MongoDB replica set and identity integration described in
[Phase 2](phase-2.md). The default app intentionally returns
`503 platform_identity_not_integrated` for these endpoints until
`create_app(verify_user_id=...)` is connected. The internal service key cannot stand
in for a logged-in candidate. All reads, corrections, history, confirmation,
evaluation and deletion enforce candidate ownership; recruiters/admins cannot
access this candidate workflow.

Configure the same provider keys, account scope and actual RPM/TPM/RPD limits on
API and generation workers. Start the CV worker independently:

```powershell
uv sync
uv run python -m app.cv.worker
```

The letter worker remains `uv run python -m app.worker`. Both reuse the same
gateway, MongoDB provider budgets/circuits and heartbeat/fencing implementation.
No new database or embedding model is required for CV import.

## API contract

| Method / path | Behavior |
|---|---|
| `POST /api/v1/cv-imports?language=fr` | Raw `application/pdf` body; `Idempotency-Key` header (8–128 characters). Returns 202 with import ID and queued status. Language `fr`/`en`, default `fr`. |
| `GET /api/v1/cv-imports/{id}` | Poll queued/failed/review/accepted status. Review includes page text, facts, evidence, warnings, provider provenance and completeness evaluation. No raw file, lease token or idempotency key is returned. |
| `PUT /api/v1/cv-imports/{id}` | Full fact replacement with `expected_version`; creates an immutable review version. See `examples/cv-corrections.json`. |
| `GET /api/v1/cv-imports/{id}/versions/{version}` | Read owned immutable fact/warning history. |
| `POST /api/v1/cv-imports/{id}/accept` | `{"expected_version": 2, "confirmed": true}`. Explicit boolean true only. Creates one validated profile atomically; replay returns the same profile ID. |
| `DELETE /api/v1/cv-imports/{id}` | Removes raw PDF, page text, draft facts/history and extraction tasks. Keeps a small ownership/hash/idempotency tombstone; later reads return 410. |
| `GET /api/v1/profiles/{id}/evaluation` | Deterministic completeness for a structured imported profile and its current version. Plain-text profiles return 409. |

For an authenticated backend caller, reuse its actual session headers in
`$platformSessionHeaders`; this service does not define a replacement token format:

```powershell
$uploadHeaders = $platformSessionHeaders + @{ 'Idempotency-Key' = 'candidate-cv-import-001' }
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/v1/cv-imports?language=fr' `
  -Headers $uploadHeaders -ContentType 'application/pdf' -InFile './candidate.pdf'
```

The frontend should explain that uploading queues external AI processing. Only
extracted page text/schema are sent to Groq or fallback Gemini, never the PDF bytes.
Raw page text still contains personal data; provider handling and retention must
be reviewed before a real-data pilot. Logs contain normalized failure categories,
not CV text, provider responses or secrets.

## Storage, limits and recovery

`sid_cv_files` stores one bounded BSON binary PDF. A 5 MiB maximum keeps the file
well below MongoDB's document size limit and allows upload/import/task insertion
in one transaction. Larger documents would require a designed GridFS/object-store
lifecycle; they are rejected in this slice. `sid_cv_imports` holds the review head,
`sid_cv_versions` its immutable history, and `sid_cv_tasks` the private leased queue.

Limits are 5 MiB upload, 10 pages, 16,000 extracted characters across all pages,
1 MiB decoded content per page, 40 facts, 240 characters per value and 400 per quote.
The parser runs in a disposable subprocess with a 15-second deadline and a 512 MiB
RSS watchdog. POSIX additionally applies an address-space ceiling. The Windows
watchdog samples RSS, so it is not a hard operating-system memory sandbox; deploy
workers with resource limits. Cancellation/timeout kills and reaps the parser.

Encrypted, malformed/truncated, oversized, image-only and insufficient-text PDFs
produce normalized errors before AI calls. Any empty page stops extraction to avoid
silently discarding scanned content. OCR is not included. Strict parsing may reject
recoverable PDFs; candidates can export a clean text PDF. Text extraction does not
execute links, JavaScript or embedded attachments. For parser behavior and memory
limitations, see the [official pypdf extraction documentation](https://pypdf.readthedocs.io/en/6.12.0/user/extract-text.html).

Upload retries with the same owner/key/file/language reuse one import. A changed
file/language with that key returns 409. Deleted imports cannot be resurrected by
replay. Failed imports require a new upload key. Transient generation failures retry
with bounded attempts; refusals and permanent PDF errors stop. Invalid generated
JSON/schema counts as a provider failure and can fall back using the existing
gateway. There is no unbounded JSON repair loop. Eligible retries reserve quota
again and may incur additional provider usage.

Multiple workers claim with fencing tokens and heartbeats. Completion atomically
checks its lease, writes a review head/history and finishes the task. A stale worker
or late result after deletion cannot recreate the import. Account status is rechecked
before processing, before AI and after generation. Deletion cannot recall text
already sent to an AI provider; there is also a small race between the last check
and starting that request.

Deleting an import does **not** delete a separately accepted profile or its history.
Those contain candidate-confirmed facts/evidence and need their own platform deletion
lifecycle before production. This endpoint is not an account-wide erasure API.

## Evidence and human review

Each extracted fact has a typed field, value, page and verbatim quote. The server
checks the quote against extracted page text and the value against that quote, with
word boundaries so Java is not supported by JavaScript. Unsupported facts are
removed and flagged. Dates remain source strings: no inferred month/day ordering,
degree equivalence, seniority or experience duration. Conflicting singular fields
require correction before acceptance; slash dates produce a review warning.

These checks establish text occurrence, not truth or correct semantic association.
An instruction embedded in the PDF can still contain a plausible qualification.
The prompt treats CV text as untrusted and common instruction patterns are flagged,
but injection detection and source quotes are not factual-accuracy guarantees.
No extracted facts become a validated profile without candidate confirmation.

Corrections contain only `field` and `value`; callers cannot forge PDF provenance.
Unchanged facts retain their evidence. Added/changed facts are marked
`candidate_asserted` with no PDF evidence. Immutable snapshots preserve the review
history. Exact-version confirmation prevents stale text acceptance; concurrent
confirmation creates one profile. Later plain-text profile edits clear structured
metadata and validation, preserving the original snapshot and existing approval
invalidation behavior.

## Completeness rubric

| Criterion | Points |
|---|---:|
| Name | 10 |
| Email or phone | 10 |
| Location | 10 |
| Target role/headline | 10 |
| At least one skill | 20 |
| Education/training | 20 |
| Work experience **or** project | 20 |

Rubric version 1 measures presence and excludes conflicting singular fields.
The response shows every weight, awarded points, populated sections and concrete
advice for missing sections. Projects receive the same credit as experience.
It does not verify contact reachability, credentials or job suitability. Language,
age, gender, nationality, photograph and marital status do not contribute to the
score. Queued/failed imports have no evaluation yet.

## Validation

Verified 4 October 2026 as part of the full 188-test regression run, with real
MongoDB/Qdrant services and the local embedding model. CV/gateway tests also passed
as a focused 44-test run. Lint and formatting checks passed.

`tests/fixtures/cv-corpus.json` is versioned synthetic French/English source text
and developer-authored expected facts. Tests construct real text PDFs in memory,
run the native isolated parser, then validate mocked extraction responses. The
English fixture includes conflicting emails, an ambiguous date and an injected
instruction. This proves parser/contract/grounding behavior, **not** a live model's
precision, recall or injection resistance. A consented/anonymized, human-scored
corpus and opt-in live provider evaluation remain pilot gates.

```powershell
docker compose -p sid-phase2 -f compose.mongodb.yml up -d --wait
$env:SID_TEST_MONGODB_URI='mongodb://127.0.0.1:27018/?replicaSet=rs0&directConnection=true'
uv run python -m pytest tests/test_cv.py tests/test_gateway.py -q
uv run ruff check app tests
uv run ruff format --check app tests
```

Integration tests use disposable test databases only and mock all paid generation.
They cover ownership, idempotency, conflicts, explicit confirmation, stale edits,
concurrent acceptance, immutable history, no overwrite, rollback, leases, deletion,
account deactivation and API errors. Existing profile/draft and reference-search
regression tests remain required.
