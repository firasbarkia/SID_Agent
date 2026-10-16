# Local AI pipeline simulation

From the repository root on Windows, with Docker Desktop running:

```powershell
uv sync --group demo
uv run python -m app.knowledge.cli download-model  # first setup only
uv run --group demo python -m scripts.simulation start
```

Open **http://127.0.0.1:8501**. API documentation: **http://127.0.0.1:8001/docs**.
The launcher starts/reuses the existing `sid-phase2` MongoDB and `sid-phase3`
Qdrant compose services, starts the API and its CV/draft workers, prepares the
small local reference index, then starts the existing Streamlit console.
Install dependencies/model once; routine starts need no provider account.

The launcher ignores `.env` and inherited provider/database settings, forces
simulated AI and binds both interfaces to localhost. MongoDB is at port 27018,
Qdrant at 6335. Only `sid_demo` and demo collections/indexes are used. Existing
demo records survive restarts; no reset or platform writes are performed.
Three synthetic identities are available in the sidebar. This environment
reuses the explicit demo authentication bypass; production authentication is
still required when using `app.main`.

## Test the intended behavior

The demo seeds a synthetic Sfax dataset in the platform's shape into its `demo_*`
collections at startup: 26 offers from 10 companies (including a closed offer and an
unverified company) and 40 candidates (including an inactive account and an
unvalidated profile). The match sync worker indexes it before the API serves its
first search, then keeps it current every 30 seconds.

On **Simulation**, click **Run pipeline checks**. This runs the actual HTTP consumer
and the scenarios below through the `/agent` endpoints. It creates a fresh sample CV,
automatically accepts that synthetic CV for the test, and generates a draft pack.
Application approval and submission are never performed by the checks. The report
includes expected outcomes, record IDs and request/response traces. Download it from
the page, or run the same checks from a terminal:

```powershell
uv run --group demo python -m scripts.simulation check
```

The CLI writes `.artifacts/simulation/latest-checks.json` and exits nonzero on
failure. A failed prerequisite stops dependent checks and leaves its trace.

| Scenario | Expected behavior |
| --- | --- |
| CV import and review | Real PDF worker reaches review before the test accepts it |
| Candidate offer search | Up to five open Sfax PFE offers, each with a compatibility score |
| Idempotent retry | Same key and payload return the same saved turn |
| Pack generation | Worker finishes on a platform offer; approval required; nothing sent |
| Profile evaluation | Completeness, strengths quoting the profile, market-based advice |
| Follow-up memory | "Tunis instead" changes the city and keeps Python and PFE |
| No invented matches | Sousse has no PFE offers, so `no_matches` with no results |
| Ambiguous request | "Sfax or Tunis" requires clarification |
| Advice and RAG | Advice retrieves references and cites at least one |
| Stale conversation | A stale revision gets HTTP 409 |
| Candidate isolation | Candidate two cannot read candidate one's conversation |
| Recruiter sourcing | Candidates ranked by compatibility, with "Matching %" explanations |
| Anonymized sourcing | Pseudonyms only: no names, contacts or cities |
| Job posting sourcing | A recruiter's own offer gives an explained top five on its skills |
| Application scoring | Repeat calls return the same score and template justification |
| Offer eligibility and versions | Closed or unverified offers 404; stale versions 409 |
| Integration gates | Matching on platform data; submission and profile sync stay off |

For manual exploration, select an identity and click **Start synthetic session**.
Candidates get an accepted Python/SQL profile and a conversation; choose a scenario,
edit the prompt if you like and send it through `/agent/chat`. Recruiters can tick
**Anonymized selection** before sending. **Candidate sourcing** (recruiter) ranks
candidates for one of the company's offers or a pasted job description, and scores
an application. **Profile workspace** has **Evaluate with AI**. **Applications**
generates packs for platform offers. **Operations** shows the matching index state.

## What this proves

PDF parsing, MongoDB transactions and queues, workers, ownership checks, version
guards, local multilingual embeddings, Qdrant retrieval for references, offers and
candidates, the compatibility rubric, anonymization and freshness re-checks all
execute for real.

Context decisions, CV extraction and generated prose are deterministic fixtures in
simulated mode. The scripted router understands a limited set of skills, cities and
follow-ups, so this does not evaluate free-form language quality, provider latency
or production identity integration. The demo data is synthetic, so rankings show
the mechanism working, not measured matching quality. Compatibility is coverage of
an offer's stated requirements, not a hiring probability.

Profile sync to the platform and application submission stay with the main backend.
Live evaluation is a separate activity when provider and authorized data inputs are
available; this launcher cannot enable it.

## Lifecycle and troubleshooting

```powershell
uv run --group demo python -m scripts.simulation status
uv run --group demo python -m scripts.simulation stop
```

Start is repeatable and reuses its own tracked processes. Stop checks PID,
creation time, command and working directory before terminating its API/UI
process trees (including Windows child interpreters).
It retains demo data and the shared database containers. Logs and process records
are in `.artifacts/simulation/`. If another app owns 8001 or 8501, start refuses
to replace it; stop that app or the older manual demo before retrying.
If start fails after launching a service, inspect `api.log`/`ui.log`, then use
the stop command before retrying. The `.local-ui` scratch launcher is separate;
use the root console and commands above for this simulation.
