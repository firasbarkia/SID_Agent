# Phase 6: evidence correction and offline evaluation

Review date: 9 October 2026. The independently implementable part of the next
increment is delivered: versioned candidate evidence corrections and a runnable
saved-output evaluation harness. Staging platform integration and a human-scored
quality baseline are still pending their external inputs. This phase is not a
production-quality approval.

## Delivered behavior

`PUT /api/v1/application-drafts/{id}/evidence` lets the owning candidate revise
existing assessments and source quotes at an exact draft version. It preserves
the job requirements, pinned profile/source snapshot and original generated
history. Each correction records its reason and actor, increments the draft
version, invalidates active approval and marks the analysis as candidate-corrected.
New approval hashes include the corrected analysis and its provenance.

Initial analysis is marked as a model proposal. Letter text remains separately
editable and requires review after a matrix correction. Source/profile changes
reject corrections against stale versions. Other users cannot read or edit the
dossier, forged quotes are rejected, and a failed history write rolls back the
transaction. Existing dossiers without the new review metadata remain supported.
See the [API and human-review instructions](ai-evaluation.md).

`python -m app.evaluation.cli` scores saved outputs and generates attributed,
hash-bound human-review forms. It never calls providers, reads credentials or
modifies persistent user data. The corpus contains 20 synthetic French/English
cases across CVs, dossiers, retrieval and memory, with separate development and
held-out partitions. Reports record corpus/case/output/prompt fingerprints,
provider/model/configuration and reference snapshot identity.

Provisional expectation metrics and human judgments stay separate. Blank forms
are not counted as completed reviews; unknown/stale/missing/duplicate outputs,
cross-run annotations and incomplete attributed reviews are rejected. The release
gate stays unassessed until product acceptance thresholds are chosen.

## Baseline observations

These are deliberately crafted fixture outputs, not measured Groq/Gemini output.
No human scores have been supplied. The first report records **0/18 completed
human reviews**, with writing quality unscored and the release gate unassessed.

| Provisional observation | Development | Held-out |
| --- | ---: | ---: |
| Cases | 10 | 10 |
| False supported evidence rows | 2 | 1 |
| Missing requirements | 1 | 1 |
| Extra CV facts after quote grounding | 0 | 2 |
| Flagged fabricated-letter phrase cases | 1 | 2 |
| Invalid citation-ID output cases | 0 | 1 |
| Forbidden memory-term retention cases | 1 | 1 |

The unrelated-evidence and negation examples still pass production quote checks;
the correction flow makes these assessments editable, rather than certifying
their truth. An injected qualification and a negated skill can pass quote-based
CV grounding. A retrieved citation ID can accompany an unsupported sentence.
Names and superseded preferences can survive keyword filtering. The corpus
makes these limitations reproducible; it does not resolve them through heuristics.

Reports and blank forms were generated in `.artifacts/evaluation/`. Their commands
and interpretation are documented in [the evaluation guide](ai-evaluation.md).
The corpus and synthetic saved run are tracked in `data/evaluation/`.

## Verification

The final full suite passed **241 tests** with disposable MongoDB/Qdrant and the
pinned embedding model, with no skips and one existing upstream TestClient
deprecation warning. Ruff lint, formatting and `git diff --check` passed. Focused
correction, history, approval, ownership and race checks passed against real
MongoDB; the offline harness tests passed without provider calls. The rebuilt
`sid-agent:phase6-2026-10-09` container passes liveness, exposes the correction
schema and rejects correction requests without an integrated identity verifier,
as UID 10001 on a read-only filesystem. CI now generates the synthetic report
after its test gate; remote CI was not run during this task.

## Remaining gates and next increment

1. **Platform integration:** obtain the repository/login and canonical-offer
   contract recorded in [the pending-input contract](platform-integration-contract.md).
   Implement the trusted adapter and revalidation slice, then exercise a real
   authenticated candidate in staging. Current default user endpoints remain
   disabled without the verifier; submission and matching remain disabled.
2. **Human-quality baseline:** collect actual saved outputs with their real
   provenance under the agreed provider budget/data policy. Review the corpus
   expectations and all output claims, adjudicate human ratings and define product
   acceptance thresholds. The shipped synthetic run is a harness regression
   baseline only. No human scoring or live-provider access was fabricated.
3. **Independent engineering next:** add queue age and worker-heartbeat observability,
   record normalized failure/quota metrics, then run the multi-user load and
   restart/recovery scenarios from the improvement plan. Measure the shared
   admission guard before raising limits or adding worker/API instances.

Retention/account deletion, MongoDB restore and dependency/container scanning
remain subsequent gates. Submission comes after its destination/reconciliation
contract; matching requires consented data and validated visibility/evaluation.
