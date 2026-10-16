# SID Agent API reference

API version **0.8.0** · facade contract **`agent-v2`** · updated 15 October 2026.

This is the integration reference for the Python AI service: every endpoint, who may
call it, its inputs and what it returns. Generated machine-readable schemas come from
`uv run python -m scripts.export_agent_contract` (`agent-v2.openapi.json`). Request
examples for every `/agent` call are in `data/contracts/requests-v2.json`.

Node.js integrates through the **`/agent` facade**. The `/api/v1` routes are the
underlying workflows (review, versions, polling, operations); they stay available
for screens that need them.

## Authentication

| Scheme | How it is sent | Who uses it |
| --- | --- | --- |
| Service key | `X-Service-Key: <SID_SERVICE_API_KEY>` | The trusted Node.js backend, never a browser. |
| Platform user | `Authorization: Bearer <platform token>`, checked by the verifier passed to `create_app(verify_user_id=...)` | Identifies the candidate or recruiter. The role, account state and company are reloaded from the platform's `users` and `company_profiles` on every request. |
| Demo identity | `X-Demo-User: demo-candidate` | Only in the localhost demo (`app.demo`). |

- Every `/agent` POST needs **both** the service key and a verified user. A service key
  never stands in for a user.
- `/api/v1` candidate and conversation routes need a verified user. Knowledge,
  operations and matching-status routes need the service key only.
- Without a configured verifier, user routes answer `503 platform_identity_not_integrated`.
- Request bodies can never choose a role, a company or a search mode.

## Idempotency

Routes that create work require `Idempotency-Key`: 8 to 128 characters from letters,
digits, `.`, `_` and `-`. Reusing a key with the same payload returns the saved result
(202 or 200); reusing it with a different payload returns 409. Use a new key for each
new logical action. `/agent/parse-cv`, `/agent/generate-pack`, `/agent/chat`,
`POST /api/v1/cv-imports`, `POST /api/v1/application-drafts`,
`POST /api/v1/application-dossiers` and conversation turns require it. Read-style
POSTs (`source-candidates`, `score-application`, `evaluate-profile`,
`candidate-refs/resolve`) do not.

## Errors

Domain errors: `{"detail": {"code": "<code>"}}`. Validation errors use FastAPI's shape:
`{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}`.

| HTTP | Meaning | Typical codes |
| --- | --- | --- |
| 401 | Missing or wrong credentials | `invalid_service_key`, `invalid_platform_identity`, `inactive_or_unknown_user` |
| 403 | Wrong role or company | `candidate_access_required`, `recruiter_access_required`, `company_profile_required` |
| 404 | Not found or not yours (existence is never revealed) | `offer_not_accessible`, `candidate_not_accessible`, `candidate_ref_not_found` |
| 409 | Stale version, changed key payload or changed data | `stale_offer_version`, `search_results_changed`, `offer_changed_since_generation`, `profile_version_not_validated`, `stale_conversation_revision`, `stale_draft_version` |
| 413 / 415 / 422 | Too large, wrong type, invalid input | `cv_file_too_large`, `invalid_pdf`, `anonymization_recruiter_only`, `empty_search_query` |
| 429 | Per-user queue limits | `user_pending_job_limit`, `user_cv_storage_limit`, `user_enqueue_rate_limit` |
| 503 | Not configured or temporarily unavailable; safe to retry | `matching_index_not_ready`, `matching_unavailable`, `matching_busy`, `platform_data_unavailable`, `conversation_ai_unavailable`, `database_unavailable` |

## Endpoint index

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | /agent/parse-cv | Service key + user (candidate) | Upload a PDF CV and queue extraction |
| POST | /agent/generate-pack | Service key + user (candidate) | Queue a tailored letter and evidence pack for a platform offer |
| POST | /agent/chat | Service key + user | One conversation turn: advice, offer search or candidate sourcing |
| POST | /agent/source-candidates | Service key + user (recruiter) | Top five candidates for an own offer or a job description |
| POST | /agent/score-application | Service key + user (recruiter) | Deterministic compatibility score for one application |
| POST | /agent/evaluate-profile | Service key + user (candidate) | Completeness, strengths and concrete advice |
| POST | /agent/candidate-refs/resolve | Service key + user (recruiter) | Turn an anonymized pseudonym back into a candidate ID |
| GET | /agent/capabilities | Service key | Which AI features are configured |
| POST | /api/v1/cv-imports | User (candidate) | Upload a PDF CV (same as `/agent/parse-cv`) |
| GET | /api/v1/cv-imports/{import_id} | User (candidate) | Read extracted facts, evidence and completeness |
| PUT | /api/v1/cv-imports/{import_id} | User (candidate) | Correct extracted facts at an exact version |
| DELETE | /api/v1/cv-imports/{import_id} | User (candidate) | Delete the import and its PDF |
| GET | /api/v1/cv-imports/{import_id}/versions/{version} | User (candidate) | Read a past import version |
| POST | /api/v1/cv-imports/{import_id}/accept | User (candidate) | Accept a version and create a SID profile |
| POST | /api/v1/profiles | User (candidate) | Create a profile from text |
| GET | /api/v1/profiles/{profile_id} | User (candidate) | Read the current profile |
| PUT | /api/v1/profiles/{profile_id} | User (candidate) | Edit at an exact version |
| GET | /api/v1/profiles/{profile_id}/versions/{version} | User (candidate) | Read a past profile version |
| POST | /api/v1/profiles/{profile_id}/accept | User (candidate) | Accept an exact version |
| GET | /api/v1/profiles/{profile_id}/evaluation | User (candidate) | Deterministic completeness only |
| POST | /api/v1/application-drafts/preview | Service key | Letter preview from supplied text; nothing stored |
| POST | /api/v1/application-drafts | User (candidate) | Queue a letter from supplied job text |
| POST | /api/v1/application-dossiers | User (candidate) | Queue a dossier from supplied job text |
| GET | /api/v1/application-drafts/{draft_id} | User (candidate) | Read a draft or pack |
| PUT | /api/v1/application-drafts/{draft_id} | User (candidate) | Edit the letter; clears approval |
| GET | /api/v1/application-drafts/{draft_id}/versions/{version} | User (candidate) | Read a past draft version |
| PUT | /api/v1/application-drafts/{draft_id}/evidence | User (candidate) | Correct evidence assessments; clears approval |
| POST | /api/v1/application-drafts/{draft_id}/approvals | User (candidate) | Record consent for a destination; sends nothing |
| GET | /api/v1/tasks/{task_id} | User (candidate) | Poll a queued generation job |
| POST | /api/v1/conversations | User (candidate or recruiter) | Start a conversation |
| GET | /api/v1/conversations/{conversation_id} | User (candidate or recruiter) | Read history and memory |
| DELETE | /api/v1/conversations/{conversation_id} | User (candidate or recruiter) | Delete a conversation and its memory |
| POST | /api/v1/conversations/{conversation_id}/turns | User (candidate or recruiter) | Same pipeline as `/agent/chat` |
| POST | /api/v1/knowledge/search | Service key | Search the reference knowledge base |
| GET | /api/v1/knowledge/status | Service key | Reference index state |
| GET | /api/v1/operations/status | Service key | Queue counts and provider state |
| GET | /api/v1/matching/status | Service key | Offer and candidate index readiness |
| GET | /api/v1/ai/status | Service key | Provider gateway and quota state |
| GET | / | Public | Service name |
| GET | /health | Public | Liveness |
| GET | /health/ready | Public | Readiness of configured dependencies |

## Agent facade

Every facade response carries `contract_version: "agent-v2"` and `submitted: false`:
the AI service never submits or publishes anything. Queued work answers 202 with a
`Location` header equal to `poll_url`.

### POST /agent/parse-cv

Upload a CV as raw PDF bytes (`Content-Type: application/pdf`, at most 5 MiB, text
PDFs only). Extraction runs in the CV worker; poll `poll_url` until `status` is
`review`, then let the candidate correct and accept it.

| Input | Where | Type | Required | Notes |
| --- | --- | --- | --- | --- |
| body | body | PDF bytes | yes | Must start with `%PDF-`; scanned images are not read |
| `language` | query | `fr` \| `en` | no | Default `fr` |
| `Idempotency-Key` | header | string | yes | See Idempotency |

Returns 202 `ParseReceipt`: `import_id`, `status`, `version`, `poll_url`,
`requires_human_approval`, `sync_status: "not_integrated"`.

### POST /agent/generate-pack

Queue a tailored application pack (evidence matrix, cover letter, CV suggestions) for
an accepted profile and an open platform offer. Job and company text are loaded by
the service from the exact offer version; callers cannot supply them.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `profile_id` | string, 1–128 | yes | An accepted SID profile owned by the candidate |
| `profile_version` | integer ≥ 1 | yes | Must equal the accepted version |
| `offer_id` | string, 1–128 | yes | Platform `job_offers.id`; must be OPEN from a verified company |
| `offer_version` | 64 hex characters | yes | The offer's content hash, as returned in search results (`version`) |
| `language` | `fr` \| `en` | no | Default `fr` |

Returns 202 `PackReceipt`: `draft_id`, `task_id`, `status`, `poll_url`, `draft_url`,
`sources` (an offer binding), `requires_human_approval: true`,
`adaptation_format: "suggestions"`. Errors: 404 `offer_not_accessible` (closed,
unverified or unknown), 409 `stale_offer_version`, 503 `platform_data_unavailable`.
Approving the draft later fails with 409 `offer_changed_since_generation` if the
offer changed after generation.

### POST /agent/chat

One conversation turn through the pipeline diagram: the context agent routes the
message to advice (reference RAG) or matching. Candidates search offers; recruiters
search validated candidates. Create the conversation first with
`POST /api/v1/conversations`.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `conversation_id` | string, 1–128 | yes | Owned by the caller |
| `expected_revision` | integer ≥ 0 | yes | Current conversation revision; stale values get 409 |
| `message` | string, 2–2000 | yes | The user's prompt |
| `anonymized` | boolean | no | Recruiters only (candidates get 422 `anonymization_recruiter_only`) |

Returns 200 `ChatReceipt`: `conversation_id`, `revision`, `message`, `created_at` and
`response`:

| Response field | Meaning |
| --- | --- |
| `status` | `answered`, `insufficient_knowledge`, `clarification_required`, `matches_found`, `no_matches` or `matching_unavailable` |
| `intent`, `query`, `keywords` | Context-agent decision and the replacement keyword memory |
| `target` | `offers` (candidate), `candidates` (recruiter) or null |
| `filters` | City, contract (`PFE`, `INTERNSHIP`, `EMPLOYMENT`) and skills, kept across follow-ups |
| `criteria` | Requirements used for compatibility (recruiter searches) |
| `results` | Up to five `OfferMatch` or `CandidateMatch` objects (see Result objects) |
| `references` | Up to five cited knowledge references (advice) |
| `text`, `explanation_source` | The answer and whether explanations came from the `model` or the deterministic `template` |
| `search_mode`, `anonymized`, `stale_dropped` | `platform` when matching ran; anonymization flag; hits dropped because their record changed |
| `routing`, `generation` | Provider and model provenance |

Replaying a key revalidates every returned record; if any changed since the turn,
the replay answers 409 `search_results_changed`. Errors: 503
`matching_index_not_ready` before the first index sync, 503
`conversation_ai_unavailable` when routing cannot reach a provider.

### POST /agent/source-candidates

Recruiter sourcing (§5.2). Give exactly one source.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `offer_id` | string, 1–128 | one of | One of the recruiter's own company offers; its required skills become the criteria |
| `job_description` | string, 20–8000 | one of | Pasted job text; criteria are the reviewed skill names found in it |
| `anonymized` | boolean | no | Rank on skills and experience only and return pseudonyms |
| `language` | `fr` \| `en` | no | Default `fr` |

Returns 200 `SourcingReceipt`: `offer` (binding or null), `criteria`, `anonymized`,
`status` (`matches_found` or `no_matches`), `text`, `results` (up to five
`CandidateMatch`, ordered by compatibility, then relevance), `explanation_source`,
`generation`, `stale_dropped`. Errors: 404 `offer_not_accessible` for another company's
offer, 403 for non-recruiters, 422 when both or neither source is given.

### POST /agent/score-application

The deterministic `compat-v1` score Node.js shows next to a received application. No
model call: repeated calls with unchanged data return identical results.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `offer_id` | string, 1–128 | yes | Must belong to the recruiter's company |
| `candidate_profile_id` | string, 1–128 | yes | Platform `candidate_profiles.id` with a validated profile |
| `language` | `fr` \| `en` | no | Language of the justification |

Returns 200 `ApplicationScore`: `offer` (binding pinning the scored offer version) and
`result` (a `CandidateMatch` with `relevance: null` and a template `explanation` such as
"Matching 70 % : couvre ReactJS, TypeScript ; à renforcer : Git."). It never changes the
application's status.

### POST /agent/evaluate-profile

The §5.1 profile evaluator for a candidate's accepted SID profile.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `profile_id` | string, 1–128 | yes | Owned by the candidate |
| `profile_version` | integer ≥ 1 | yes | Must be the current, accepted version (else 409 `profile_version_not_validated`) |
| `language` | `fr` \| `en` | no | Default `fr` |

Returns 200 `ProfileAssessment`: `completeness` (deterministic score, breakdown,
missing-field advice), `strengths` (each with a verbatim `quote` from the profile),
`advice` (each with `basis`: `missing_field`, `market_gap` or `wording`),
`market_gaps` (requirements most often missing across the 12 closest open offers,
with counts), `assessment_source`, `generation`, `cached` (results are reused for one
hour per profile version and language), `created_at`.

### POST /agent/candidate-refs/resolve

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `candidate_ref` | `C-` + 10 hex | yes | A pseudonym issued to the caller's company |

Returns 200 `CandidateRefResolution` with `candidate_profile_id`. Another company's
pseudonym answers 404. Call it only when the recruiter acts (contact, invite).

### GET /agent/capabilities

Service key only. Returns which features are configured: `identity_integrated`,
`database_configured`, `advice_configured`, `offer_search`, `candidate_sourcing`,
`application_scoring`, `profile_evaluator`, `pack_offer_resolution` (`platform` or
disabled), `matching_index_ready`, `anonymization`, `profile_sync: "not_integrated"`,
`submission_enabled: false`. This is configuration, not a quality certificate.

## Result objects

**OfferMatch** (`kind: "offer"`): `id`, `version` (content hash), `title`, `contract`,
`company` (`id`, `name`, `sector`, `city`), `description`, `requirements`, `relevance`
(0–1 cosine), `compatibility` (null without a candidate profile), `score`
(0.6 × relevance + 0.4 × compatibility, or relevance alone), `explanation`,
`explanation_source`, `evidence` (a quote from the offer), `binding`.

**CandidateMatch** (`kind: "candidate"`): `anonymized`, `candidate_id` (null when
anonymized), `candidate_ref` (pseudonym when anonymized), `display_name` (name, or the
pseudonym), `city` (null when anonymized), `skills`, `highlights` (up to three
experience lines with names, emails, phones and links removed), `is_new`,
`relevance`, `compatibility`, `score`, `explanation`, `explanation_source`,
`evidence`, `binding` (`candidate_version`).

**Compatibility** (`compat-v1`): `percent` (0–100), `criteria` (per requirement:
`level` = `demonstrated` 1.0, `listed` 0.8, `related` 0.4 or `missing` 0, with
`similarity` and the matched `evidence`), `gaps`, `rubric_version`,
`model_fingerprint`, and the label "Coverage of the offer's stated requirements, not
a hiring probability." ReactJS counts toward "Développement Frontend" through the
reviewed alias table (`data/matching/skill-aliases-v1.json`).

**Offer binding**: `kind: "platform_offer"`, `offer_id`, `offer_version`,
`company_id`, `company_version`, `snapshot_sha256`. Node.js can recheck it before it
submits an application.

## CV imports and profiles

### POST /api/v1/cv-imports

Same inputs and behavior as `/agent/parse-cv`; returns the full `CVImportView`.

### GET /api/v1/cv-imports/{import_id}

Returns `CVImportView`: `status` (`queued`, `review`, `accepted`, `failed`), `version`,
`facts` (field, value, origin, page evidence), `pages`, `warnings`, `evaluation`
(completeness), provenance and timestamps.

### PUT /api/v1/cv-imports/{import_id}

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `expected_version` | integer ≥ 1 | yes | Current import version |
| `facts` | list of 1–40 `{field, value}` | yes | `field`: name, email, phone, location, headline, skill, education, experience, project or language; `value`: 1–240 characters. Replaces the fact list. |

### DELETE /api/v1/cv-imports/{import_id}

Deletes the import, its stored PDF and any unfinished job. Returns 204.

### GET /api/v1/cv-imports/{import_id}/versions/{version}

Returns an immutable past version.

### POST /api/v1/cv-imports/{import_id}/accept

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `expected_version` | integer ≥ 1 | yes | The version the candidate reviewed |
| `confirmed` | `true` | yes | Explicit confirmation |

Creates a SID profile from the accepted facts and returns the import with `profile_id`.

### POST /api/v1/profiles

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `content` | string, 20–12000 | yes | Profile text; `field: value` lines are read as facts |
| `external_profile_id` | string, 1–128 | no | Optional platform profile reference |

### GET /api/v1/profiles/{profile_id}

Returns the current profile, its `version` and `validated_version`.

### PUT /api/v1/profiles/{profile_id}

`expected_version` (integer ≥ 1) and `content` (20–12000). Editing resets acceptance.

### GET /api/v1/profiles/{profile_id}/versions/{version}

Returns a past profile version.

### POST /api/v1/profiles/{profile_id}/accept

`expected_version` and `confirmed: true`, as for CV acceptance.

### GET /api/v1/profiles/{profile_id}/evaluation

Deterministic completeness only. Use `/agent/evaluate-profile` for strengths, advice
and market gaps.

## Drafts, dossiers and approvals

### POST /api/v1/application-drafts/preview

Service key only; nothing is stored. Inputs: `candidate_profile` (20–12000),
`job_description` (20–8000), `company_context` (≤ 4000, optional), `language`.
Returns a generated letter.

### POST /api/v1/application-drafts

Queue a cover letter from text the candidate supplies (for offers outside the
platform). Inputs: `profile_id`, `profile_version` (accepted), `job_description`
(20–8000), `company_context` (≤ 4000), `language`, plus `Idempotency-Key`. Returns 202
with `draft_id` and `task_id`.

### POST /api/v1/application-dossiers

Same inputs as above; queues a full dossier (evidence matrix, letter, CV suggestions).

### GET /api/v1/application-drafts/{draft_id}

Returns `DraftView`: `status`, `version`, `cover_letter`, `dossier_analysis`
(evidence and CV suggestions), `source_snapshot` (kind `platform_offer` for facade
packs), `requires_human_approval`, `approval_id`.

### PUT /api/v1/application-drafts/{draft_id}

`expected_version` and `cover_letter` (20–20000). Clears any approval.

### GET /api/v1/application-drafts/{draft_id}/versions/{version}

Returns an immutable past draft version.

### PUT /api/v1/application-drafts/{draft_id}/evidence

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `expected_version` | integer ≥ 1 | yes | Current draft version |
| `evidence` | list of 1–12 rows | yes | Same requirements and job quotes, in order; each row: `requirement` (≤ 240), `job_quote` (≤ 400), `candidate_quote` (≤ 400 or null), `assessment` (`supported` or `not_evidenced`) |
| `reason` | string, 5–400 | yes | Why the candidate corrected it |

### POST /api/v1/application-drafts/{draft_id}/approvals

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `expected_version` | integer ≥ 1 | yes | The version approved |
| `confirmed` | `true` | yes | Explicit consent |
| `destination_id` | string, 1–128 | yes | Where Node.js will send it |

Records consent only (201). Node.js submits the application.

### GET /api/v1/tasks/{task_id}

Returns `TaskStatus`: `state` (`pending`, `running`, `done`, `failed`), `attempts`,
`error_code`, `draft_id`.

## Conversations

### POST /api/v1/conversations

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `language` | `fr` \| `en` | no | Default `fr` |
| `profile_id`, `profile_version` | string, integer | no, together | Candidates only: pins an accepted profile used to rank and score offers |

Returns 201 with the conversation `id` and `revision: 0`.

### GET /api/v1/conversations/{conversation_id}

Private history (turns with saved responses) and keyword memory.

### DELETE /api/v1/conversations/{conversation_id}

Deletes the conversation, its turns and memory. Returns 204.

### POST /api/v1/conversations/{conversation_id}/turns

Body `expected_revision`, `message`, `anonymized` and `Idempotency-Key`; same pipeline
and response as `/agent/chat`.

## Knowledge, operations and health

### POST /api/v1/knowledge/search

Service key. `query` (2–500), `limit` (1–10, default 5), `source` (`esco`, `rome`,
`onet`, `geonames`, optional), `kind` (`skill`, `occupation`, `location`, optional).
Cosine similarity over reference records; not a compatibility score.

### GET /api/v1/knowledge/status

Active reference generation and counts.

### GET /api/v1/operations/status

Queue counts by state, expired leases and provider circuit state.

### GET /api/v1/matching/status

`configured`, then per index (`offers`, `candidates`): `ready`, `collection`,
`records`, `skipped` (malformed platform records), `last_sync_at`, `last_indexed`,
`last_removed`.

### GET /api/v1/ai/status

Provider configuration, circuits and quota buckets.

### GET /

Service name.

### GET /health

Process liveness (the container health check).

### GET /health/ready

Configuration and database readiness. It makes no paid provider calls.

## Platform data the AI service reads

Read-only, in the platform's UML shape. Collection names come from
`PLATFORM_USERS_COLLECTION`, `PLATFORM_CANDIDATES_COLLECTION`,
`PLATFORM_COMPANIES_COLLECTION` and `PLATFORM_OFFERS_COLLECTION`.

| Collection | Fields read | Rule |
| --- | --- | --- |
| `users` | `id`, `role`, `isActive`, `email` | Active users only; email only for display when not anonymized |
| `company_profiles` | `id`, `userId`, `companyName`, `sector`, `address`, `description`, `isVerified` | Offers are searchable only for verified companies; the city is the last known Tunisian city in `address` |
| `job_offers` | `id`, `companyId`, `title`, `type`, `description`, `requiredSkills`, `status` | `status: OPEN`; `type` maps STAGE_PFE → PFE, STAGE_PERIOD → INTERNSHIP, EMPLOI → EMPLOYMENT |
| `candidate_profiles` | `id`, `userId`, `fullName`, `phone`, `city`, `rawCvUrl`, `parsedSkills`, `parsedExperiences`, `parsedProjects`, `parsedEducation`, `isNewCandidate` | Discoverable when `parsedSkills` is non-empty and the user is an active candidate. Name, phone, email and CV URL never enter vectors, payloads or prompts. |

`requiredSkills` and `parsedSkills` may be lists of strings or objects with `name`,
`skill`, `label` or `title`, a dictionary, or delimited text. A version is the SHA-256
of the projected fields, because the UML has no version field.

## Local demo only

These exist only in `app.demo` (localhost, synthetic identities):

| Method | Path | Purpose |
| --- | --- | --- |
| GET | /demo/status | Demo mode, AI mode, identities, workers and matching index state |
| GET | /demo/workspace | The identity's profiles, drafts, imports and conversations |
| GET | /demo/offers | Open offers with current versions (recruiters: their own company's) |
| POST | /demo/knowledge/setup | Import a small reference subset and build a test index |
