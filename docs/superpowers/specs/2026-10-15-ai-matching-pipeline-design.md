# AI matching pipeline design (§5 and the pipeline diagram)

Date: 15 October 2026. Status: approved in conversation, section by section.
Sources: functional specification §5 and the AI pipeline diagram (see
[requirement sources](../../requirements.md)). Platform login (JWT verifier) and
staging accounts are out of scope; the localhost demo identities stand in for them.

## Goal

Implement the remaining AI features of §5 along the pipeline diagram:

- **Candidate branch:** conversational offer search over real platform offers, with
  vector search composed from the prompt, the user profile and conversation keywords,
  a top five with explanations, and keyword memory.
- **Company branch:** recruiter sourcing from a natural-language query or a job
  posting, a semantic compatibility percentage, an explained top five, and optional
  anonymized selection.
- **Scoring:** the same compatibility rubric for received applications.
- **Profile evaluator:** completeness plus grounded strengths and concrete advice.
- **Tailored pack:** generated against real platform offers pinned by content hash.

Already delivered and kept: context agent, advice RAG, CV parsing and review, letters,
CV suggestions, evidence corrections, approvals, conversation memory and leases.

## Decisions

| Topic | Decision |
| --- | --- |
| Data source | Read the platform's UML collections read-only: `job_offers`, `candidate_profiles`, `company_profiles`, `users` (names configurable). The demo seeds a reviewed Sfax dataset into its `demo_*` copies, so demo and production run the same code. |
| Discoverable candidates | Active users with role CANDIDATE whose `candidate_profiles` record has non-empty `parsedSkills` (only written after the candidate validates the profile). |
| Searchable offers | `status: OPEN` offers whose company profile has `isVerified: true`. |
| Versions | The UML has no version field. A record's version is the SHA-256 of its normalized projection. |
| Engine | Versioned Qdrant collections embedded with the existing local multilingual model, kept current by a sync worker; every hit is reloaded and rechecked against MongoDB at query time. |
| Compatibility | Deterministic rubric `compat-v1`; the LLM only writes explanations. |
| Anonymization | Recruiter switch per search; identity never enters vectors, payloads or prompts. |
| Contract | Payloads move to `contract_version: agent-v2` (offer versions become hash strings). Paths are unchanged. No consumer is integrated yet. |

## Components

All new code lives in `app/matching/` unless noted.

### Catalog (`catalog.py`)

Read-only projection of platform records into typed documents.

- `OfferDoc`: `id`, `company_id`, `title`, `contract` (STAGE_PFE → PFE,
  STAGE_PERIOD → INTERNSHIP, EMPLOI → EMPLOYMENT), `description`, `requirements`
  (from `requiredSkills`: strings or objects with `name`/`skill`/`label`), `company`
  (`name`, `sector`, `city`, `description`), `version` (projection hash),
  `company_version` (company projection hash).
- `CandidateDoc`: `id`, `user_id`, `skills`, `statements` (experience, project and
  education lines flattened to text), `city`, `is_new`, `version`. Identity
  (`fullName`, `phone`, user `email`, `rawCvUrl`) is loaded separately by
  `identity()` and is never part of the projection or hash input used for search.
- City: the company `address` (or candidate `city`) folded to lowercase ASCII tokens.
- `eligible_offers()`, `eligible_candidates()`, `offer(id)`, `candidate(id)` return
  `None` for ineligible records. `candidate_for_user(user_id)` serves the candidate
  branch.

### Skill aliases (`data/matching/skill-aliases-v1.json`)

Reviewed groups of equivalent names (ReactJS ≈ React ≈ React.js) and implications
(React implies frontend and JavaScript). Normalization: casefold, strip accents,
collapse punctuation and whitespace.

### Index and sync (`index.py`, `worker.py`)

- Two Qdrant collections per generation: `sid_match_offers_<hex>` and
  `sid_match_candidates_<hex>`. `QdrantIndex` accepts the `sid_match_` prefix,
  configurable payload index fields, and generic filters.
- Each record becomes chunks of at most 128 tokens: a headline chunk (title or role
  plus skills) and description chunks. Payload: `record_id`, `version`, `kind`,
  `contract`, `company_id`, `city_tokens`. No names, contacts or free identity text.
- `sid_match_records` (MongoDB) holds, per record, the indexed version, the
  professional projection and float32 phrase vectors for requirements, skills and
  statements, so the rubric needs no embedding at query time for indexed records.
- `sid_match_state` holds the active collection per kind with the embedder
  fingerprint, the last sync time and counts, and a lease so only one worker writes.
- A sync cycle (default every 30 s): project eligible records, embed and upsert only
  changed versions, delete points and records that disappeared or became ineligible.
  A fingerprint change builds a new collection and switches when the build completes.
- `python -m app.matching.worker` runs the loop; the demo runs it in-process.

### Rubric (`rubric.py`)

- Criteria: offer requirements, or skills extracted from a recruiter query.
- For each criterion: alias match or implication gives similarity 1.0; otherwise the
  cosine similarity of phrase vectors. Compared against the candidate's skills
  (listed) and statements (demonstrated), with lexical containment counting as 1.0.
- Levels: demonstrated 1.0 (statement ≥ high threshold), listed 0.8 (skill ≥ high
  threshold), related 0.4 (either ≥ low threshold), missing 0. Thresholds are
  calibrated against labelled pairs with the real model and recorded in the rubric.
- Percentage = round(100 × mean points). Each criterion keeps its level, similarity
  and evidence phrase; missing criteria are gaps.
- Every result carries `rubric_version`, the embedder fingerprint and the label
  "coverage of the offer's stated requirements, not a hiring probability".

### Engine (`engine.py`)

- Query vector: normalize(0.60 × prompt + 0.25 × profile or company context + 0.15 ×
  conversation keywords).
- Retrieval: Qdrant with contract and city payload filters, 30-record pool, grouped by
  record; each hit reloaded from the catalog and dropped if its version changed or it
  became ineligible (counted as `stale_dropped`).
- Candidate branch: semantic skill filter on offer requirements; final score =
  0.6 × relevance + 0.4 × compatibility when the candidate has a profile, otherwise
  relevance. Top five.
- Company branch: candidates ordered by compatibility, relevance breaking ties. In
  anonymized mode the city filter is ignored. Top five.
- `resolve_offer` for packs, `score` for applications, `market_gaps` for the evaluator.
- Query embedding runs in a thread with bounded concurrency (busy → 503
  `matching_busy`).

### Anonymization (`anonymize.py`)

- Pseudonym: `C-` + first 8 hex of HMAC-SHA256(key, company_id + candidate_id); key
  derived from the service key with a fixed label. Stable per company, unlinkable
  across companies.
- Scrub: remove the candidate's name tokens, emails, phone numbers and URLs from any
  text before prompts and results. City is not returned.
- `sid_candidate_refs` records issued pseudonyms per company;
  `POST /agent/candidate-refs/resolve` returns the profile ID only to a recruiter of
  that company.

### Explanations (conversation service)

- Prompt builder per the diagram: history, top five with criteria, profile or company
  context, current prompt. The LLM returns `MatchAnswer` (text, evidence quotes).
- Validation: evidence IDs equal the returned IDs, quotes occur in the record's
  (scrubbed) text, and every percentage in the text equals a computed score.
- If generation fails after provider fallback (`ProvidersUnavailable`, `ServiceBusy`,
  refusals), a deterministic French or English template explains each result and the
  response carries `explanation_source: template`. Ranking never depends on the LLM.

### Profile evaluator

`POST /agent/evaluate-profile` for a validated SID profile version:

- Completeness: the existing deterministic `evaluate()`.
- Market gaps: requirements most often missing or only related across the 12 offers
  closest to the profile, e.g. "Docker is required in 8 of the 12 closest offers".
- Strengths and advice: LLM output where every strength quotes the profile and advice
  cites a missing field, a market gap or a wording issue; deterministic fallback.
- Cached per profile version and language for one hour in `sid_profile_assessments`.

## API (agent-v2)

All `/agent` POST routes need `X-Service-Key`, a verified user and an
`Idempotency-Key` where they create records; roles are checked on the server.

| Route | Change |
| --- | --- |
| `POST /agent/chat` | Matching returns `OfferMatch` or `CandidateMatch` (discriminated by `kind`) with relevance, compatibility, explanation and source binding. Optional `anonymized` (recruiters only; 422 for candidates). Replays revalidate every returned record (409 `search_results_changed`). |
| `POST /agent/generate-pack` | `offer_version` is the offer content hash. Resolves real platform offers; binding kind `platform_offer`. Approval rejects a pack whose offer changed (409 `offer_changed_since_generation`). |
| `POST /agent/source-candidates` (new) | Recruiter: `offer_id` (own company) or `job_description`, `anonymized`, `language`. Explained top five. |
| `POST /agent/score-application` (new) | Recruiter: `offer_id` + `candidate_profile_id`. Deterministic rubric and template justification, no LLM. |
| `POST /agent/evaluate-profile` (new) | Candidate: `profile_id`, `profile_version`, `language`. |
| `POST /agent/candidate-refs/resolve` (new) | Recruiter: `candidate_ref` issued to their company. |
| `GET /agent/capabilities` | Reports offer search, candidate sourcing and evaluator as `platform` or `disabled`. |
| `GET /api/v1/matching/status` (new, service key) | Active collections, counts, last sync, lag. |

Errors: 503 `matching_index_not_ready`, `matching_unavailable`, `matching_busy`;
404 `offer_not_accessible`, `candidate_not_accessible`; 409 `stale_offer_version`,
`search_results_changed`, `offer_changed_since_generation`; 403 role or company.

## Testing

- Unit: projection and hashing, aliases, rubric levels and calibration pairs
  (including ReactJS ≈ "Développement Frontend"), query composition, scrubbing,
  explanation validation, template fallback.
- Integration (MongoDB, Qdrant, real model): sync create/update/close/delete,
  unverified company and inactive user, filters, stale-hit drop, company ownership,
  anonymized responses and captured prompts free of identity, score determinism,
  evaluator grounding, pack resolution and approval recheck.
- Demo and simulation: Sfax PFE search on seeded data, recruiter ReactJS sourcing,
  anonymized mode, application scoring, profile evaluation.

## Out of scope

JWT verifier and staging accounts; fair rotation and the new-profile boost (§4C);
sending accepted profiles to Node.js; live-provider quality runs and human labels.
