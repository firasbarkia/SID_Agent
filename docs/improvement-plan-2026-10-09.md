# System improvement plan, 9 October 2026

The four confirmed defects from the 8 October review have been addressed. The
next objective is a measurable staging workflow: one authenticated candidate
imports or creates a profile, corrects and accepts an exact version, generates a
dossier against a verified offer, and reviews its claims. Keep submission and
matching disabled until their own contracts and acceptance gates pass.

This is the proposed next work, not a claim that the items below are implemented.
The accompanying [review](review-2026-10-09.md) records evidence and remaining limits.

## 1. Establish the main-platform contract and implement one staging slice

Inputs required: the platform repository or sanitized contracts for login,
collections/IDs, account revocation, company membership, offer publication and
versioning. Choose the authoritative source of each rule. Record token expiry,
revocation and cookie CSRF/CORS behavior if applicable. Confirm a replica-set
deployment and least-privilege database permissions.

Implement a trusted identity adapter and a read-only canonical offer adapter,
then connect one candidate review flow. Bind the dossier to an accessible offer
ID/version and company version; revalidate access and source versions when
generating and approving. Show source quotes, model proposals and editable letter
text clearly. Do not consume approval events as submission commands.

Acceptance: staging tests cover revoked/expired identity, inactive users, wrong
ownership, changed/deleted/private offers, stale approval and duplicate enqueue.
One real authenticated candidate completes import, correction, acceptance,
generation and review. Submission remains disconnected. Organization membership
and candidate discovery must be specified before recruiter features are enabled.

## 2. Build an AI evaluation harness and evidence correction flow

This can start before the platform contract arrives. Create synthetic French and
English cases covering CV extraction, dossier evidence, letters and retrieval.
Include unrelated quotes, negation, ambiguous dates, absent facts, contradictory
requirements, multilingual aliases, prompt injection and invented company claims.
Keep a held-out set and record model, prompt, reference snapshot and provider
configuration for every run. Live provider runs need actual account access/budgets;
the offline harness should also accept saved outputs.

Use human labels for claim support, citation relevance and letter factuality.
Measure unsupported supported-labels, missed requirements, extraction errors,
sentence-level citation support, PII in memory, refusal/retry behavior and writing
quality by language. Field completeness stays separate from qualification or
compatibility. Synthetic validator tests cannot substitute for these measurements.

Add versioned evidence corrections so a candidate can reject or revise an
assessment without inventing a new qualification in their profile. Preserve the
original model output, the correction and source versions. Analysis edits must
invalidate approval and be included in the next approval hash.

Acceptance: a human-scored baseline and held-out report exist; unsupported claims
are visible and correctable; correction/history/stale-version tests pass. Set
release thresholds with the product owner before testing candidate models. No
automatic hiring decision or compatibility percentage is introduced.

## 3. Measure capacity and recovery, then tune admission

Add latency distributions, queue age, worker heartbeats, normalized failure
counts, quota rejections and provider reservations. Keep identities and source
text out of metrics. Make stale worker heartbeats distinguishable from an empty
queue. Require consistent admission policy across API instances; consider storing
that policy centrally, following the existing provider-policy pattern.

Run reproducible multi-user load and failure tests: oversized/chunked/slow bodies,
concurrent CV/dossier bursts, admission contention, worker kills, parser resource
failures, expired leases, provider downtime and database interruption. Measure
peak memory, throughput, transaction retries, recovery time and per-user wait.
Configure ingress connection/header/request-rate limits and confirm a worker
supervisor restart/alert policy in the actual deployment.

Acceptance: publish a report with agreed latency, backlog and recovery targets;
prove no oversubscription, lost approved versions or cross-user access. Tune the
defaults from evidence. Define tenant fairness from platform organization rules
before replacing FIFO scheduling or splitting shared admission locks.

## 4. Complete storage lifecycle and prove MongoDB restore

Decide retention separately for original PDFs, extracted text, profiles, drafts,
immutable history, conversations, approval records, outbox events and backups.
The present CV byte cap bounds original files; accepted profiles, history and
unconsumed events need their own policy. Include provider data retention and
account deletion requirements in that decision.

Implement scheduled retention and account deletion with ownership and late-worker
fencing. Preserve only the idempotency/audit metadata allowed by policy. Establish
backup protection and recovery objectives. Restore a disposable MongoDB backup
and verify ownership, profiles/history, approval invalidation, queues and admission
against restored authoritative records; rebuild Qdrant from its source metadata.

Acceptance: retention/deletion tests pass, a documented restore drill succeeds,
and backup/provider handling matches the chosen policy. A Qdrant restore alone
does not satisfy this gate.

## 5. Add maintained dependency and container scanning

Run a supported dependency scanner against the lockfile and a container scanner
against the built runtime image in CI. Keep action/tool versions pinned, define
severity handling and time-bounded exceptions, and save scan reports. Refresh
base-image digests through reviewed changes. Resolve the existing TestClient
deprecation through the upstream-supported migration with API regression checks.

Acceptance: CI produces reproducible scan reports, fails according to the agreed
policy and exercises Windows/Linux checksum-preserving checkout. No clean scan
is presented as a guarantee of safety.

## 6. Enable submission only against a verified delivery contract

After the staging and lifecycle gates, specify application creation, destination
authorization, canonical source versions, idempotency, retry semantics and
ambiguous-delivery reconciliation. Revalidate exact approval, identity and offer
access immediately before delivery. Add an explicit review/send UI and immutable
delivery audit records.

Acceptance: tests cover timeout after destination success, duplicate delivery,
revoked approval, offer changes, lost leases and reconciliation after restart.
Enable for a small monitored cohort only after those tests pass.

## 7. Evaluate matching in shadow mode

Use consented candidate data and verified accessible offers. Establish product
filters and an evaluation rubric before implementing ranking. Compare to a simple
baseline, review explanation support and assess relevance by language and user
group. Resolve discoverability, recruiter organization access and retention first.

Acceptance: visibility and consent tests pass and a human-labelled relevance
report supports the product's agreed gate. Start with reviewed suggestions;
introduce compatibility scores only if their meaning and calibration are validated.

## Recommended next increment

Start steps 1 and 2 together: obtain the real contracts while building the offline
evaluation harness. Follow with capacity/recovery and lifecycle work before
submission. This order exposes integration and quality failures before expanding
the product's ability to act on candidates' behalf.

## Progress after the next increment

The [Phase 6 review](phase-6.md) records the delivered offline harness and
versioned candidate evidence-correction flow. Twenty synthetic cases and blank
human-review forms are available; a human-scored/live-provider baseline remains
pending. The [platform contract inputs](platform-integration-contract.md) are
documented and the repository location has been requested; no unverified login or
offer adapter was enabled. Steps 1 and 2 therefore remain partially complete.

Next: complete those external gates when their inputs arrive, while implementing
step 3's queue-age/worker-heartbeat observability and measured recovery scenarios.
Steps 4–7 remain planned work.
