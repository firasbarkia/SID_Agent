# AI Matching Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement §5's candidate and company matching branches, compatibility scoring, anonymized sourcing and the profile evaluator over real platform data, following the AI pipeline diagram.

**Architecture:** A read-only catalog projects the platform's UML collections into typed documents with content-hash versions. A sync worker embeds them with the existing local model into versioned Qdrant collections and stores phrase vectors in MongoDB. A matching engine composes query vectors, retrieves and rechecks candidates, applies the deterministic `compat-v1` rubric, and the conversation service and `/agent` routes explain results with validated LLM output or a template fallback.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, PyMongo async, Qdrant REST, ONNX MiniLM (`LocalEmbedder`), pytest.

**Spec:** `docs/superpowers/specs/2026-10-15-ai-matching-pipeline-design.md`

## Global Constraints

- Platform collections are read-only; SID writes only `sid_*` collections and `sid_match_*` Qdrant collections.
- Identity fields (`fullName`, `phone`, user `email`, `rawCvUrl`) never enter vectors, Qdrant payloads, `sid_match_records` or LLM prompts.
- Versions are SHA-256 of the normalized projection; payload `contract_version` is `agent-v2`.
- Compatibility percentages come only from `compat-v1`; text percentages must equal computed scores.
- Query vector weights 0.60 prompt / 0.25 profile or company context / 0.15 keywords; candidate final score 0.6 relevance + 0.4 compatibility; pool 30; top five.
- Rubric points: demonstrated 1.0, listed 0.8, related 0.4, missing 0.
- Ruff line length 100; tests run offline by default, MongoDB/Qdrant/model tests opt in via `SID_TEST_*`.

## Review Focus

1. Platform documents with unexpected shapes (no `id`, `requiredSkills` as objects, dict or null, missing company) — the record is skipped and counted; sync never crashes. Test: `test_projection_tolerates_odd_shapes`.
2. A record changes or closes between indexing and query — it is dropped, never shown; replaying an older turn returns 409. Tests: `test_stale_hit_dropped`, `test_replay_revalidates_results`.
3. A candidate's name or email inside experience text in anonymized mode — scrubbed from prompts and results. Test: `test_anonymized_prompt_and_response_have_no_identity`.
4. Empty index, no profile, or no criteria — explicit status or `compatibility: null`, never a 500. Tests: `test_index_not_ready_is_503`, `test_search_without_profile_has_no_compatibility`.
5. LLM explanation with invented IDs or percentages — rejected and replaced by the template. Test: `test_invalid_explanation_falls_back_to_template`.

---

### Task 1: Catalog, aliases and seed data

**Files:**
- Create: `app/matching/aliases.py`, `app/matching/catalog.py`, `app/matching/seed.py`
- Create: `data/matching/skill-aliases-v1.json`, `data/contracts/platform-seed-v1.json`
- Test: `tests/test_matching_catalog.py`

**Interfaces:**
- Produces: `normalize_term(text) -> str`; `AliasTable.load(path=None)`, `.canonical(term) -> str`, `.implied(term) -> set[str]`, `.equivalent(a, b) -> bool`; `OfferDoc`, `CandidateDoc`, `CompanyInfo` (Pydantic); `PlatformCatalog(db, settings)` with async `eligible_offers()`, `eligible_candidates()`, `offer(id)`, `candidate(id)`, `candidate_for_user(user_id)`, `identity(candidate_id) -> dict`; `seed_platform(db, settings) -> dict` (idempotent upsert into configured collections).

- [ ] Write tests: contract mapping (STAGE_PFE→PFE …), version changes iff projection changes, identity excluded from projection and hash, unverified company or CLOSED offer → `None`, inactive or non-candidate user → `None`, empty `parsedSkills` → `None`, odd shapes skipped (`test_projection_tolerates_odd_shapes`), alias equivalence (ReactJS ≈ React.js, "Développement Frontend" canonical `frontend`, React implies frontend), seed idempotency with a fake async collection.
- [ ] Run them, confirm failures.
- [ ] Implement modules and data files (about 30 offers / 10 companies / 40 candidates; Sfax plus Tunis and Sousse; closed, unverified and inactive edge cases; the original fixture offers folded in).
- [ ] Run tests to green; ruff.

### Task 2: Index store, Qdrant extension and sync worker

**Files:**
- Modify: `app/knowledge/qdrant.py` (allow `sid_match_`, `fields` on `create`, `filters` on `query`)
- Modify: `app/config.py` (`match_sync_seconds=30`, `match_pool_size=30`, `match_max_concurrent_queries=2`)
- Modify: `app/db/connection.py` (indexes for `sid_match_records`, `sid_match_state`, `sid_candidate_refs`, `sid_profile_assessments`)
- Create: `app/matching/index.py`, `app/matching/worker.py`
- Test: `tests/test_matching_index.py`

**Interfaces:**
- Consumes: `PlatformCatalog`, `LocalEmbedder`-like `chunks/encode/fingerprint/dimensions`, `QdrantIndex`.
- Produces: `MatchIndex(db, qdrant, embedder)` with async `sync(kind, docs) -> dict`, `active(kind) -> str` (raises `ValueError` when none), `records(kind, ids) -> dict[id, record]`, `status() -> dict`; `MatchSyncWorker(db, qdrant, embedder, settings).run_once() -> dict`; `python -m app.matching.worker`.

- [ ] Write tests with a fake Qdrant and fake embedder: first sync creates collection and upserts every chunk; unchanged second sync embeds nothing; edited record re-embedded and old points deleted; removed or ineligible record deleted; fingerprint change builds a new collection and switches; payload contains no identity; lease prevents concurrent writers. Integration variant with real MongoDB+Qdrant.
- [ ] Implement; tests green; ruff.

### Task 3: Compatibility rubric

**Files:**
- Create: `app/matching/rubric.py`
- Test: `tests/test_matching_rubric.py`

**Interfaces:**
- Produces: `Phrase(text, vector)`; `Rubric(aliases, high, low)`; `Rubric.score(criteria: list[Phrase], skills: list[Phrase], statements: list[Phrase]) -> Compatibility`; `Compatibility(percent, rubric_version, fingerprint, criteria: list[Criterion], gaps, label)`; `Criterion(requirement, level, points, similarity, evidence)`; `template_justification(compatibility, language) -> str`.

- [ ] Write tests: each level with constructed vectors, alias and implication give 1.0, lexical containment in a statement gives demonstrated, percent rounding, gaps listed, empty criteria → `None`, calibration with the real model (ReactJS vs "Développement Frontend" at least related; Python vs Comptabilité missing).
- [ ] Implement; calibrate thresholds with the real model and record them; tests green.

### Task 4: Engine and anonymization

**Files:**
- Create: `app/matching/engine.py`, `app/matching/anonymize.py`
- Modify: `app/matching/models.py` (agent-v2 result models)
- Test: `tests/test_matching_engine.py`

**Interfaces:**
- Produces: `MatchEngine(db, catalog, index, embedder, rubric, settings)` with async `search_offers(principal, query, *, profile, keywords, filters) -> SearchResult`, `search_candidates(principal, query, *, criteria, context, keywords, filters, anonymized) -> SearchResult`, `resolve_offer(principal, offer_id, version) -> OfferDoc`, `score(principal, offer_id, candidate_id) -> CandidateMatch`, `revalidate(principal, results)`, `market_gaps(profile_doc, limit=12) -> list`; `Anonymizer(key).pseudonym(company_id, candidate_id)`, `.scrub(text, identity)`; models `OfferMatch`, `CandidateMatch`, `PlatformOfferBinding`, `SearchResult`.

- [ ] Write tests with fakes: weighted query composition, contract/city filters passed to Qdrant, semantic skill filter, stale hit dropped (`test_stale_hit_dropped`), candidate ordering by blended score, recruiter ordering by compatibility, no profile → `compatibility is None` (`test_search_without_profile_has_no_compatibility`), anonymized ignores city and returns pseudonyms with scrubbed evidence, pseudonym stable per company and different across companies, `resolve_offer` 404/409 cases, `matching_index_not_ready` when no generation.
- [ ] Implement; tests green.

### Task 5: Candidate branch, chat contract v2 and packs

**Files:**
- Modify: `app/conversations/service.py`, `app/conversations/models.py` (`TurnRequest.anonymized`), `app/api/conversations.py`, `app/contracts/models.py`, `app/api/agent.py`, `app/main.py`, `app/dossier.py`, `app/db/store.py`
- Delete: `app/matching/synthetic.py`, `tests/test_synthetic_matching.py`
- Test: rework `tests/test_agent_contract.py`; add `tests/test_matching_conversation.py`

**Interfaces:**
- Consumes: `MatchEngine`, `template_justification`.
- Produces: `app.state.match_engine`; `ConversationResponse.results: list[OfferMatch | CandidateMatch]`; `explanation_source`; `GeneratePackRequest.offer_version: str`; `PlatformPackDraft`; approval recheck.

- [ ] Write tests: candidate chat returns real top five with relevance and compatibility; explanation validation rejects foreign IDs, missing quotes and wrong percentages and falls back to template (`test_invalid_explanation_falls_back_to_template`); provider outage → template; replay revalidates (`test_replay_revalidates_results`); `anonymized` from candidate → 422; index missing → 503 (`test_index_not_ready_is_503`); generate-pack resolves platform offer, 404 closed/unverified, 409 stale hash; approval 409 after offer edit; OpenAPI shows agent-v2.
- [ ] Implement; tests green.

### Task 6: Company branch endpoints

**Files:**
- Modify: `app/api/agent.py`, `app/api/operations.py`, `app/conversations/service.py`
- Test: `tests/test_recruiter_matching.py`

**Interfaces:**
- Produces: `POST /agent/source-candidates`, `POST /agent/score-application`, `POST /agent/candidate-refs/resolve`, `GET /api/v1/matching/status`, recruiter matching in `/agent/chat`, capabilities fields.

- [ ] Write tests: recruiter chat sourcing returns candidates ordered by compatibility with "Matching NN%" text; source-candidates by own offer and by job description; other company's offer → 404; candidate role → 403; score-application deterministic across calls; anonymized prompt and response free of identity (`test_anonymized_prompt_and_response_have_no_identity`); resolve works only for issuing company; status reports counts.
- [ ] Implement; tests green.

### Task 7: Profile evaluator

**Files:**
- Create: `app/matching/evaluator.py`
- Modify: `app/api/agent.py`, `app/contracts/models.py`
- Test: `tests/test_profile_evaluator.py`

**Interfaces:**
- Produces: `POST /agent/evaluate-profile` → `ProfileAssessment(completeness, strengths[{text, quote}], advice[{text, basis}], market_gaps, assessment_source, cached)`.

- [ ] Write tests: strengths must quote the profile (invalid quote → fallback), advice basis limited to `missing_field`/`market_gap`/`wording`, market gaps counted from nearby offers, cache hit within one hour, unvalidated profile version → 409, recruiter → 403.
- [ ] Implement; tests green.

### Task 8: Demo, simulation, console, contracts and docs

**Files:**
- Modify: `app/demo.py` (seed platform data, run sync worker, simulated outputs for new schemas, status), `scripts/simulation.py`, `examples/agent_consumer.py`, `examples/simulation_checks.py`, `streamlit_app.py`, `scripts/export_agent_contract.py`, `data/contracts/requests-v1.json` → `requests-v2.json`
- Modify docs: `docs/agent-api.md`, `docs/operations.md`, `README.md`; create `docs/api-reference.md` (every endpoint, auth, inputs, outputs, errors)
- Test: update `tests/test_demo.py`, `tests/test_simulation.py`

- [ ] Update simulation scenarios: Sfax PFE search on seeded data, follow-up city change, recruiter ReactJS sourcing, anonymized sourcing, application scoring, profile evaluation, pack on platform offer.
- [ ] Full suite with MongoDB, Qdrant and the model; ruff; run the simulation checks against a live demo.

### Task 9: Review, gap check and API documentation

- [ ] Run a code review over the branch; fix confirmed findings.
- [ ] Re-check the delivery-map gaps against the new behavior and update docs.
- [ ] Generate the endpoint inventory from OpenAPI and reconcile `docs/api-reference.md`.
