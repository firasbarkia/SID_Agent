# Reviewable application dossiers

Delivered 8 October 2026. The existing candidate draft worker and approval flow
now produce a cover letter, evidence matrix and CV suggestions. They use a
validated SID profile version and candidate-supplied job/company text, which is
not yet bound to a verified platform offer or company record.

## API flow

All routes require the platform's trusted user verifier and candidate role.
After creating and accepting a profile, queue a dossier:

```http
POST /api/v1/application-dossiers
Idempotency-Key: application-dossier-001
Content-Type: application/json

{
  "profile_id": "<accepted-profile-id>",
  "profile_version": 1,
  "job_description": "PFE internship requiring Python in Sfax.",
  "company_context": "",
  "language": "en"
}
```

The `202` response contains `draft_id` and `task_id`. Run `python -m app.worker`
separately. Poll `GET /api/v1/tasks/{task_id}`, then read
`GET /api/v1/application-drafts/{draft_id}`. The existing plain-letter endpoint
keeps its behavior. Reusing an idempotency key across different content or between
a plain letter and a dossier returns `409`.

A completed dossier includes:

- `cover_letter`: editable text, initially requiring human approval.
- `dossier_analysis.evidence`: copied job requirements, exact job/candidate quotes
  and `supported` or `not_evidenced` assessments.
- `dossier_analysis.cv_suggestions`: suggestions tied to existing candidate quotes.
- `source_snapshot`: source kind, profile ID/version and SHA-256 hashes of profile,
  job and company text. Original text remains in immutable source versions.
- `analysis_letter_version`: letter version generated alongside the analysis.
- Provider/model/fallback provenance, version and approval status.

The shared Groq/Gemini gateway validates JSON and quoted evidence before accepting
a response. Invalid output can trigger fallback and bounded task retries. Missing
requirements use `not_evidenced` with no candidate quote. A quote proves text
occurrence, not competence, semantic relevance or factual truth. The letter and
suggestions still require human review. Empty company context is handled in the
prompt; deterministic tests do not establish live quality.

Edit the letter using `PUT /api/v1/application-drafts/{id}` with
`expected_version` and `cover_letter`. Editing invalidates existing approval and
sets `analysis_letter_version` to `null`: source analysis remains available but
has not been regenerated for the edited letter. To correct source analysis,
the candidate can edit/accept their profile and request a new dossier.

After review, `POST /api/v1/application-drafts/{id}/approvals` requires
`expected_version`, `confirmed: true` and `destination_id`. Approval binds the
letter, source profile version, job/company text, analysis, source hashes and
destination. Profile changes invalidate approval and reject stale generation.
Approval records consent only: no application is sent, and the destination ID
is not yet checked against a canonical platform offer.

## Remaining integration

The platform repository or sanitized contracts are needed for login verification,
offer/company versions and visibility, and submission with stable idempotency and
ambiguous-delivery reconciliation. Implement a sender only against that contract.
Do not consume `draft.approved` events as submission instructions until destination
validation is integrated. Matching remains disabled as requested.

A human-labelled corpus and live provider evaluation remain necessary for
unsupported claims, company personalization, French/English writing quality and
evidence relevance. Current tests use synthetic generation responses.
