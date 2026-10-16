# Platform integration contract: pending inputs

This repository currently supplies `create_app(verify_user_id=...)`, the
`UserIdVerifier` callback and a read-only `PlatformReader`. The verifier must return
an authenticated user ID; roles/account state are subsequently loaded from MongoDB.
The reader assumes string `id` / `userId` / `companyId`, `isActive` and an owned
company profile. These are supplied UML assumptions, not a verified live contract.

No main-platform repository or authoritative login/offer contract was available
during this phase. The user has been asked for a local path or repository URL.
Do not invent a JWT secret, trust a browser-supplied user ID, infer publication from
document existence or enable submission from approval events to fill this gap.

| Contract | Required evidence | Implementation seam |
| --- | --- | --- |
| Login | Token/session mechanism, issuer/audience/verification, expiry, revocation, account state, staging credentials/test principal | `app/identity.py` callback and `app/main.py` identity resolver |
| User/organization | Actual ID types/collections, role authority, organization memberships, recruiter permissions | `app/platform.py` principal and company reads |
| Candidate profile | Canonical ownership/version fields, discoverability/consent, update/deletion rules | Profile import and source snapshots |
| Offer/company | Publication/private visibility, candidate access, canonical versions, company changes and deleted-offer behavior | New read-only offer binding for dossier request, worker and approval |
| Database | Replica-set topology, transaction support, database ownership and least-privilege credentials | Existing `mongo_connection` / `ensure_indexes` |
| Browser/session | Frontend origin, cookies or bearer tokens, CSRF and CORS rules where applicable | Platform factory and frontend review flow |
| Delivery | Authorized application destination, idempotency, confirmation and reconciliation semantics | Remains a later phase; submission disabled |

First staging acceptance: one authenticated candidate creates/imports, corrects
and accepts an exact profile version, generates against one accessible canonical
offer and reviews the dossier. Test expired/revoked identity, inactive accounts,
wrong ownership, private/deleted/changed offers, stale approval and duplicate work.
Matching now reads the platform's UML collections read-only; the exact fields and
eligibility rules are listed in the [API reference](api-reference.md#platform-data-the-ai-service-reads).
Confirm those field names, `isVerified` semantics and the discoverability rule
(validated `parsedSkills`) with the backend team. Submission stays with Node.js.
Capture the resolved fields and test evidence
here when the real contract becomes available.
