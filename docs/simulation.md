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

On **Simulation**, click **Run pipeline checks**. This runs the actual HTTP
consumer through all three `/agent/*` endpoints. It creates a fresh sample CV,
automatically accepts that synthetic CV for the test, and generates a draft
pack. Application approval and submission are never performed by the checks.
The report includes expected outcomes, record IDs and request/response traces.
Download it from the page, or run the same checks from a terminal:

```powershell
uv run --group demo python -m scripts.simulation check
```

The CLI writes `.artifacts/simulation/latest-checks.json` and exits nonzero on
failure. A failed prerequisite stops dependent checks and leaves its trace.

| Scenario | Expected behavior |
| --- | --- |
| Sample PDF | Real upload/extraction worker reaches review; exact-version acceptance creates profile |
| Python PFE in Sfax | Only `fixture-python-pfe`, maximum five results, synthetic ranking/evidence |
| Retry same chat request/key | Same response; no duplicate turn |
| “Tunis instead” | Change city; retain Python + PFE; return `fixture-tunis-pfe` |
| Python PFE in Sousse | `no_matches`; no invented opportunity |
| Sfax or Tunis | `clarification_required` |
| Advice on Python skills | Advice branch retrieves local references and returns citations |
| Stale conversation revision | HTTP 409 |
| Second candidate reading first candidate's history | HTTP 404 |
| Recruiter searching candidates | `matching_unavailable`; private discovery disabled |
| Pack with private/closed offer or stale version | HTTP 404 or 409 before queueing |
| Pack with accepted profile + eligible offer | Real worker; source versions pinned; human approval required |
| Integration capabilities | Real private matching/submission disabled; profile sync `not_integrated` |

For manual exploration, select an identity and click **Start synthetic session**.
This accepts a sample Python/SQL profile for candidates and creates a conversation.
Choose a scenario, edit its prompt if desired and send it through `/agent/chat`.
Try the Sfax search followed by the location-change scenario in the same session.
The page shows routing, response status, saved memory, results and full JSON.
**Conversation** shows history; **CV import** lets you review/correct a PDF yourself;
**Applications** shows generated packs and their evidence/approval controls.

## What this proves

PDF parsing, MongoDB transactions/queues, workers, ownership checks, version
guards, API serialization, local multilingual reference embeddings and Qdrant
retrieval all execute. Seven synthetic offers exercise the candidate search
adapter, including public, closed and private examples.

Context decisions, CV extraction and generated prose are deterministic fixtures.
The scripted router understands the listed prompts and a limited set of skills,
cities and follow-ups. This does not evaluate free-form language quality,
provider latency, real matching quality or production identity integration.
Synthetic offer ranking uses the fixture token-vector adapter, not the neural
reference encoder. Similarity is not a compatibility percentage.

Recruiter discovery, real private matching, profile sync and application
submission remain gated. Live evaluation is a separate activity when provider
and authorized data inputs are available; this launcher cannot enable it.

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
