# Requirement sources

The JCI requirement documents are not stored in git because this repository is
public. Keep local copies in `specs/` at the repository root; that folder is
ignored. Ask the project lead for the files if you don't have them.

| Expected local file | Document | Pages | Covers |
| --- | --- | ---: | --- |
| `specs/Cahier des Charges - Plateforme Emploi & Marketplace.pdf` | Functional specification (Cahier des Charges Fonctionnel, MVP) | 3 | Project vision, MVP objectives, actors, candidate/company/fair-visibility modules, AI features, post-MVP roadmap |
| `specs/APIs Backend.pdf` | Backend API specification (Node.js and Python services) | 2 | Node.js modules A-E and the three Python `/agent/...` endpoints |
| `specs/ai-pipeline-diagram.jpg` | AI pipeline activity diagram | 1 | Context agent, advice RAG, role-specific vector matching, prompt builders, LLM, keyword memory, top-five response |

The supplied UML class diagram is already tracked as
`WhatsApp Image 2026-09-24 at 16.35.45.jpeg` (see [Phase 2](phase-2.md)).

## Requirement index

A short paraphrase for navigation. The PDFs remain the authority.

| Source section | Requirement | Tracked in |
| --- | --- | --- |
| Functional spec §1 | Territorial platform for the Sfax region, led by JCI, with a marketplace pole and a job-fair pole | [Roadmap: sources and ownership](ai-scope-and-api-roadmap-2026-10-12.md#sources-and-ownership) |
| Functional spec §2 | MVP is the intelligent Job Fair: faster matching, equal visibility, an AI assistant for candidates and companies | [Roadmap: scope audit](ai-scope-and-api-roadmap-2026-10-12.md#scope-audit-before-ab-implementation) |
| Functional spec §3 | Actors: candidate, company recruiter, JCI administrator | [Platform integration contract](platform-integration-contract.md) |
| Functional spec §4A | Candidate sign-up, CV parsing, profile review before saving, applications space | [Agent API](agent-api.md), [Phase 4](phase-4.md) |
| Functional spec §4B | Company profile, offer management, received applications with an AI compatibility score | [Roadmap: increment C](ai-scope-and-api-roadmap-2026-10-12.md#c-complete-recruiter-sourcing-explanations-and-scoring) |
| Functional spec §4C | Fair rotation and a temporary boost for new profiles and offers | [Roadmap: increment C](ai-scope-and-api-roadmap-2026-10-12.md#c-complete-recruiter-sourcing-explanations-and-scoring) |
| Functional spec §5 | Human-in-the-loop: nothing is submitted or published without the user's confirmation | [Phase 5](phase-5.md) |
| Functional spec §5.1 | Profile autofill, tailored pack (cross-analysis, letter, CV optimization), profile evaluator, conversational offer search | [Agent API](agent-api.md), [Phase 5](phase-5.md) |
| Functional spec §5.2 | Recruiter sourcing, semantic compatibility percentage, explained top five, optional anonymized selection | [Roadmap: increment C](ai-scope-and-api-roadmap-2026-10-12.md#c-complete-recruiter-sourcing-explanations-and-scoring) |
| Functional spec §6 | Post-MVP: B2B marketplace, investment space, virtual booths | Out of current scope |
| API spec §1, modules A-E | Node.js auth/profiles, jobs, applications and scoring, conversations and internal parsed-data sync, company news | [Platform integration contract](platform-integration-contract.md) |
| API spec §2 | Python `POST /agent/parse-cv`, `POST /agent/generate-pack`, `POST /agent/chat` | [Agent API](agent-api.md) |
