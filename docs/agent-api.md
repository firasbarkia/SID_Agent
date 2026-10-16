# Agent API v2: AI pipeline handoff

Delivered 15 October 2026. This increment implements the AI features of §5 of the
functional specification along the AI pipeline diagram, on the platform's own data
model. It replaces the synthetic offer fixtures of agent-v1. Platform login (the JWT
verifier) and staging accounts remain the main backend's integration step.

Full inputs, outputs and error codes: [API reference](api-reference.md). Design:
[matching pipeline spec](superpowers/specs/2026-10-15-ai-matching-pipeline-design.md).

## What the pipeline does

| Diagram stage | Implementation |
| --- | --- |
| Prompt → context agent | Classifies advice, matching or clarify; extracts city, contract and skills; keeps them across follow-ups |
| Advice → RAG → prompt builder → LLM | Reference search (ESCO, ROME, O*NET, GeoNames) with cited answers |
| Matching → user type → target | Candidates search **offers**; recruiters search **candidates** |
| Vector search (profile + keywords + prompt) | Query vector = 0.60 prompt + 0.25 profile or company context + 0.15 conversation keywords, searched in versioned Qdrant indexes built with the local multilingual model |
| Prompt builder → LLM | The top five with their computed criteria; the model may only quote returned records and repeat computed percentages |
| Keyword memory → response + top 5 | Saved in one transaction with the turn; replays revalidate every record |

| §5 feature | Endpoint |
| --- | --- |
| 5.1 Profile autofill and validation | `/agent/parse-cv`, then CV correction and acceptance under `/api/v1/cv-imports` |
| 5.1 Tailored pack (cross-analysis, letter, CV optimization) | `/agent/generate-pack` on a platform offer |
| 5.1 Human validation before sending | Draft edits, evidence corrections and approvals; `submitted` is always false |
| 5.1 Profile evaluator | `/agent/evaluate-profile` |
| 5.1 Conversational offer search | `/agent/chat` as a candidate |
| 5.2 Sourcing from a query or a job posting | `/agent/chat` as a recruiter, `/agent/source-candidates` |
| 5.2 Semantic matching and percentage | The `compat-v1` rubric on every result |
| 5.2 Top five with explanations | Every search; explanations validated or replaced by a template |
| 5.2 Anonymized selection | `anonymized: true`, with `/agent/candidate-refs/resolve` |
| §4B AI score on received applications | `/agent/score-application` |

## Data and freshness

The service reads the platform's UML collections read-only (`job_offers`,
`company_profiles`, `candidate_profiles`, `users`); see the field table in the API
reference.
- **Searchable offers:** open offers from verified companies.
- **Discoverable candidates:** active candidates with a validated profile (`parsedSkills`).
- **Versions:** the UML has no version field, so a record's version is the SHA-256 of its projected fields.

`python -m app.matching.worker` keeps the indexes current: every 30 seconds by
default it re-embeds only changed records and removes closed, deleted or ineligible
ones. Every search re-reads its hits from MongoDB and drops any whose version
changed, so closed offers and edited profiles never reach a prompt or a result.

## Compatibility rubric `compat-v1`

Each requirement of the offer (or each skill a recruiter asks for) is matched against
the candidate's skills and experience lines:

| Level | Points | When |
| --- | ---: | --- |
| demonstrated | 1.0 | An experience or project line names it (or an equivalent) |
| listed | 0.8 | A listed skill is the same or an equivalent |
| related | 0.4 | Embedding similarity ≥ 0.50 |
| missing | 0 | None of the above |

The percentage is the mean of the points. Equivalences come from the reviewed alias
table `data/matching/skill-aliases-v1.json` (ReactJS ≈ React, and React counts toward
"Développement Frontend"). Thresholds were calibrated on the local model on 15
October 2026: unrelated short terms reached 0.42, while short technical synonyms were
often below 0.5, so the reviewed table carries technical synonyms. The percentage
describes coverage of stated requirements, never a hiring probability.

Candidates' offer results combine relevance and compatibility (0.6 / 0.4). Recruiter
results are ordered by compatibility, then relevance.

## Anonymized selection

With `anonymized: true` a recruiter search ignores the city filter, ranks on skills
and experience only, and returns pseudonyms (`C-` + 10 hex). A pseudonym is stable
within one company and unlinkable across companies. Names, phone numbers, emails and
CV links are removed from candidate text before it is stored for search, so they
never reach prompts or results in either mode. `/agent/candidate-refs/resolve`
returns the profile ID only to the issuing company.

## Explanations

The model receives only returned records. Its answer is rejected, and a deterministic
French or English template is used instead (`explanation_source: "template"`), when:
- it names an unknown ID,
- it misses a result,
- a quote does not appear in the record's text,
- it states a percentage that is not the computed one,
- no provider is available.

Ranking and scores never depend on the model.

## Changes from agent-v1

- `contract_version` is `agent-v2`; `offer_version` is a 64-character content hash.
- Search results are `OfferMatch` or `CandidateMatch` objects with `relevance`,
  `compatibility`, `explanation` and a binding; `search_mode` is `platform`.
- Packs bind `platform_offer` sources; approval rechecks the offer version.
- New: `/agent/source-candidates`, `/agent/score-application`,
  `/agent/evaluate-profile`, `/agent/candidate-refs/resolve`,
  `GET /api/v1/matching/status`; `anonymized` on chat turns.
- Removed: the synthetic offer corpus and `search_generation`.

## Contract artifacts and examples

```powershell
uv run python -m scripts.export_agent_contract
```

The export writes `.artifacts/agent-contracts/agent-v2.openapi.json` and
`profile-sync-v1.schema.json`. `data/contracts/requests-v2.json` has one validated
example per request, and `data/contracts/platform-seed-v1.json` is the demo's
synthetic Sfax dataset in the platform's shape.

With the simulated demo running, run `uv run python -m examples.agent_consumer`. It
uploads a CV, accepts it, searches Sfax PFE offers, replays the turn and queues a pack
for the top offer. The simulation checks cover 17 scenarios, including recruiter
sourcing, anonymization, scoring and profile evaluation.

## Still with the main platform

- **JWT verifier:** login token verification and staging accounts.
- **Profile sync:** sending accepted profiles to `PATCH /internal/candidate/:id/parsed-data`.
- **Submission and listings:** application submission, and fair rotation and the new-profile boost in listings (§4C).
- **Live AI quality:** live-provider evaluation with human labels.
