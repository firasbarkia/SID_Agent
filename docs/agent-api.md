# Agent API v1: A/B contract handoff

Delivered 12 October 2026. This increment implements A's independent contract
work and B's synthetic candidate-search slice. It does not connect real login,
resolve real canonical offers, synchronize platform profiles, enable recruiter
matching or establish live AI quality. The unresolved platform contracts remain
in [the input register](platform-integration-contract.md).

## Delivered endpoints

| Endpoint | Input | Output / supporting workflow |
| --- | --- | --- |
| `POST /agent/parse-cv?language=fr` | Raw `application/pdf`, candidate identity, service key, idempotency key. | 202 `ParseReceipt`: import ID, current status/version, polling URL, review flag, `sync_status: not_integrated`, `submitted: false`. Reuses the private CV upload workflow. |
| `POST /agent/generate-pack` | Accepted SID profile ID/version, offer ID/version, language; candidate identity and both keys. | 202 `PackReceipt`: draft/task IDs, polling/draft URLs, pinned synthetic offer/company versions and snapshot hash. The default app returns 503 `canonical_offers_not_integrated`. Explicit synthetic injection resolves fixture offers and queues the existing dossier worker. |
| `POST /agent/chat` | Owned conversation ID, expected revision, message; verified candidate/recruiter identity and both keys. | Typed `ChatReceipt`: conversation ID/revision, status, text, intent, references or offer matches, filters, memory and model/index provenance. Reuses conversation leases/transactions. |
| `GET /agent/capabilities` | Service key. | Reports configured identity/database/advice, synthetic-or-disabled offer search, pending synchronization and disabled real matching/submission. This is configuration information, not a quality/readiness certificate. |

The facade uses `contract_version: agent-v1`. Successful queue requests include a
`Location` header matching `poll_url`. Replay may return the same ID with its
current processing state; 202 alone does not mean a fresh job was created.

Create conversations explicitly with `POST /api/v1/conversations`, optionally
pinning an owned accepted profile ID/version. Then use `/agent/chat` for turns.
This avoids creating duplicate conversations when a first-turn response is lost.
Node.js conversation/message ID mapping is still a pending contract.

CV correction and exact-version acceptance continue through
`PUT /api/v1/cv-imports/{id}` and `POST /api/v1/cv-imports/{id}/accept`. Profile
acceptance creates a SID profile; it does not publish/synchronize the canonical
platform profile. Pack letter/evidence edits and explicit approvals remain in the
existing `/api/v1/application-drafts/...` workflow. Approval is consent only.

Typed polling schemas now describe CV import results (facts/page evidence/
completeness), task status and the core draft result (letter/evidence/source
snapshot). Existing draft metadata remains available for backward compatibility.
CV and pack workers still run separately from the production API.

## Authentication and errors

The three POST facade operations require `X-Service-Key` plus a user verified by
`create_app(verify_user_id=...)`. The principal's role/account state is reloaded
from platform records. A service credential never substitutes for user identity.
The proposed OpenAPI transport is a bearer token passed to the verifier; the
facade does not invent or locally decode platform JWTs. The actual verifier,
issuer/audience/revocation rules and ID fields must come from the main backend.

OpenAPI describes service key and user token together, not as alternative ways
to authorize a user operation. The localhost demo explicitly uses its synthetic
`X-Demo-User` identity adapter; that exception is not enabled by `app.main`.
Request bodies cannot select a role, switch search mode or supply authoritative
job/company text to `/agent/generate-pack`.

`Idempotency-Key` is required for each POST, 8-128 characters from letters,
digits, `.`, `_` or `-`. Scope is the existing owned workflow; reuse with changed
content returns 409. Use a new key for each new logical action.

Domain failures use `{"detail":{"code":"..."}}`. Input validation uses FastAPI's
`{"detail":[...]}` shape; both are described in facade schemas. Typical failures:

| HTTP | Examples / caller action |
| --- | --- |
| 401 / 403 | Invalid service/token, wrong role; refresh identity or correct access. |
| 404 | Wrong ownership or inaccessible offer; do not assume existence. |
| 409 | Changed key payload, stale profile/offer/revision, busy conversation, changed search generation; refresh/review current state. |
| 413 / 415 / 422 | Size/content-type/invalid PDF, rejected schema or blocked generation; correct input. |
| 503 | Identity/database/offer integration missing, provider/retrieval unavailable or capacity exhausted. Configuration gaps need setup; transient failures can retry the same logical request/key. |

Raw PDF uploads retain the existing 5 MiB cap, parser isolation and body timeout/
admission controls. Chat retains the 90-second total pipeline deadline and
120-second lease. A provider charge cannot be undone if a request fails before
commit; retries of uncommitted work may call the model again.

## Synthetic offer search

`app.main` defaults to `synthetic_offer_search=None`, so both private matching
branches remain unavailable. Only explicit `SyntheticOfferSearch` injection is
currently supported. `app.demo` injects the checked-in synthetic corpus; no request
body or environment flag can switch the production app to a real private index.

Candidate matching uses this in-memory, generation-bound fixture index. The
corpus includes public/closed/private offers, multiple locations/contracts and
reviewed fixture aliases. Explicit location, contract and required-skill filters
run before ranking. Returned records are checked for current OPEN/PUBLIC status
and exact source version before explanation and again before committing/replaying
a turn. Replacement/deletion rebuilds the index and changes its fingerprint.

The 128-dimensional deterministic token-hash encoder combines normalized query,
accepted profile and keyword vectors with weights 0.60/0.25/0.15. Profile/memory
enrich ranking; they never relax hard filters or create qualifications. A shared
skill term is also required so hash collisions alone cannot make a match. Return
at most five actual corpus IDs, ordered by ranking and stable ID for ties.

Results expose `source: synthetic`, `score_type: synthetic_cosine_similarity` and
`is_compatibility_percentage: false`. This encoder is a reproducible contract
fixture, not a measured semantic model or an AI compatibility percentage. Real
versioned Qdrant candidate/job indexes and semantic evaluation remain future work.

The context schema now includes typed filters. The simulated demo router preserves
filters across follow-up turns, supports the fixture locations/skills/contracts,
and clarifies alternative locations or negation instead of pretending to interpret
them. Its small vocabulary is not general natural-language quality evidence.
Live routing still uses the existing provider gateway/schema and requires evaluation.

The second LLM step explains only retrieved IDs and must quote every returned
offer's description. Invented IDs, duplicate IDs and unsupported quotes are
rejected through existing gateway validation/fallback. These checks establish
text occurrence; overall semantic/factual explanation quality is still unmeasured.
No-results responses skip explanation generation. Recruiter requests still return
`matching_unavailable` with target `candidates`, even in the demo.

Matching statuses are `matches_found`, `no_matches` or `matching_unavailable`;
ambiguous requests can return `clarification_required`. Advice keeps its existing
`answered` / `insufficient_knowledge` outcomes. Successful turns save response,
revision and replacement memory together. Replays make no new model call, but a
changed fixture generation returns 409 instead of resurfacing stale matches.
Private GET history remains a historical record rather than a current search.

## Contract artifacts and consumer example

Generate OpenAPI and the proposed accepted-profile synchronization schema:

```powershell
uv run python -m scripts.export_agent_contract
```

Outputs: `.artifacts/agent-contracts/agent-v1.openapi.json` and
`profile-sync-v1.schema.json`. Export starts no services and makes no AI calls.
CI exports the same schemas after its regression checks.

`data/contracts/offers-v1.json` is the seven-offer synthetic corpus.
`data/contracts/requests-v1.json` supplies pack/chat/profile-sync examples.
The proposed sync envelope names the canonical candidate and expected platform
version, accepted SID profile/version, structured facts, source hash, explicit
confirmation and optional embedding reference (model/dimensions/index/source
version). Delivery headers/idempotency/error semantics must be agreed with Node.js;
no sender or platform PATCH is implemented in A/B.

Run the localhost demo as documented in the README, in simulated mode. Then:

```powershell
uv run python -m examples.agent_consumer
```

The consumer uploads a generated synthetic PDF, polls/correctly accepts the review
version, creates a pinned conversation, searches Sfax PFE Python offers, verifies
an idempotent turn replay and queues/polls a pack through the real workers. It
refuses remote origins and live demo AI mode. It uses only synthetic data and
does not approve/send the pack. Repeated runs leave new synthetic demo records;
regression tests run the same consumer in disposable MongoDB databases.

## Verification and remaining gates

The final full suite passed **282 tests**, with **no skips** and one existing
upstream TestClient deprecation warning, in 196.36 seconds. MongoDB, Qdrant and the
downloaded embedding model were enabled; all generation responses were mocked.
Ruff lint/formatting and whitespace checks passed. The new contract/search checks
exercise facade security,
OpenAPI, workers, source binding, hard filters, aliases, profile enrichment,
follow-up filters, no results, ambiguity, bounded ranking, changes/deletions,
cross-user isolation, replay and invalid evidence. The consumer example is also
exercised against the demo's actual CV/draft workers.

```powershell
$env:SID_TEST_MONGODB_URI='mongodb://127.0.0.1:27018/?replicaSet=rs0&directConnection=true'
$env:SID_TEST_QDRANT_URL='http://127.0.0.1:6335'
$env:SID_TEST_EMBEDDING_DIRECTORY='.models/multilingual-minilm'
uv run python -m pytest -q --tb=short --junitxml=.artifacts/agent-contracts/pytest-final.xml
uv run ruff check app tests streamlit_app.py examples/agent_consumer.py scripts/export_agent_contract.py
uv run ruff format --check app tests streamlit_app.py examples/agent_consumer.py scripts/export_agent_contract.py
```

The rebuilt `sid-agent:ab-2026-10-12` image passed liveness, schema/authentication
and default-capability checks as UID 10001 on a read-only filesystem. Its default
user facade rejects requests without an integrated verifier. The temporary smoke
container was removed. Remote CI was not run during this task.

An intermediate concurrent build/test run had four MongoDB fixture-setup timeouts
(277 passed, 4 setup errors). Each affected check passed separately, and the final
282-test run passed without a concurrent build. Test timeout settings were not
relaxed. The clean final JUnit report is in `.artifacts/agent-contracts/`.

A's external contract decisions remain open: real authentication/IDs/memberships,
offer visibility and versions, accepted-profile sync, conversation mapping,
adapted-CV output and application-score transport. B's real private indexes and
live-data access stay disabled. C's recruiter sourcing/compatibility/fair exposure,
D's canonical synchronization/pack handoff, and E's staging/operational gates
remain follow-up work. Live evaluation was not run because agreed provider budget,
data handling and human-scored acceptance inputs are not yet available.
