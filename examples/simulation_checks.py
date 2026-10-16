"""Behavior scenarios shared by the simulation console and command-line runner."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from app.demo import DEMO_SERVICE_KEY
from examples.agent_consumer import run

# Scenario names and expected behavior, in the order the checks run.
SCENARIOS = [
    ("CV import and review", "Real PDF worker reaches review before the test accepts it"),
    ("Candidate offer search", "Up to five open Sfax PFE offers, ranked with compatibility"),
    ("Idempotent retry", "Same key and payload return the same saved turn"),
    ("Pack generation", "Worker finishes on a platform offer; approval required; not sent"),
    ("Profile evaluation", "Completeness, strengths quoting the profile, market-based advice"),
    ("Follow-up memory", "City becomes Tunis; Python and PFE stay active"),
    ("No invented matches", "Sousse has no PFE offers, so no_matches with no results"),
    ("Ambiguous request", "Conflicting cities require clarification"),
    ("Advice and RAG", "Advice branch retrieves references and cites at least one"),
    ("Stale conversation", "A stale revision is rejected with HTTP 409"),
    ("Candidate isolation", "Candidate two cannot read candidate one's conversation"),
    ("Recruiter sourcing", "Candidates ranked by compatibility with Matching % explanations"),
    ("Anonymized sourcing", "Pseudonyms only: no names, contacts or cities"),
    ("Job posting sourcing", "A recruiter's own offer gives an explained top five on its skills"),
    ("Application scoring", "Repeat calls give the same score and template justification"),
    ("Offer eligibility and versions", "Closed/unverified offers 404; stale versions 409"),
    ("Integration gates", "Matching on platform data; submission and profile sync stay off"),
]
EXPECTED = dict(SCENARIOS)


async def run_checks(base_url, *, transport=None):
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "mode": "simulated",
        "passed": False,
        "checks": [],
        "records": {},
        "trace": [],
    }

    async def trace(response):
        await response.aread()
        request = response.request
        content = request.content
        if content and request.headers.get("content-type") == "application/pdf":
            body = f"Synthetic PDF ({len(content)} bytes)"
        else:
            body = json.loads(content) if content else None
        report["trace"].append(
            {
                "method": request.method,
                "path": request.url.raw_path.decode(),
                "identity": request.headers.get("X-Demo-User"),
                "idempotency_key": request.headers.get("Idempotency-Key"),
                "request": body,
                "status": response.status_code,
                "response": response.json(),
            }
        )

    def check(scenario, passed):
        report["checks"].append(
            {
                "scenario": scenario,
                "status": "passed" if passed else "failed",
                "expected": EXPECTED[scenario],
            }
        )
        if not passed:
            raise RuntimeError(f"{scenario}: expected {EXPECTED[scenario]}")

    try:
        # Reuse the actual consumer flow: PDF -> review -> accept -> chat -> pack.
        result = await run(
            base_url, transport=transport, event_hooks={"response": [trace]}, verbose=False
        )
        report["records"] = {
            name: result[name]
            for name in ("profile_id", "conversation_id", "import_id", "draft_id")
        }
        found = result["turn"]["response"]
        check("CV import and review", True)
        check(
            "Candidate offer search",
            found["status"] == "matches_found"
            and 0 < len(found["results"]) <= 5
            and all(
                r["contract"] == "PFE" and r["company"]["city"] == "sfax" for r in found["results"]
            )
            and all(r["compatibility"] is not None for r in found["results"]),
        )
        check("Idempotent retry", True)
        check("Pack generation", result["pack"]["sources"]["kind"] == "platform_offer")

        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=100,
            transport=transport,
            trust_env=False,
            headers={"X-Service-Key": DEMO_SERVICE_KEY, "X-Demo-User": "demo-candidate"},
            event_hooks={"response": [trace]},
        ) as client:

            async def post(path, body, *, user="demo-candidate", expected=200):
                response = await client.post(
                    path,
                    json=body,
                    headers={"X-Demo-User": user, "Idempotency-Key": "sim-" + uuid4().hex},
                )
                if response.status_code != expected:
                    raise RuntimeError(
                        f"{path}: expected HTTP {expected}, got {response.status_code}"
                    )
                return response.json()

            conversation = result["conversation_id"]
            evaluation = await post(
                "/agent/evaluate-profile",
                {"profile_id": result["profile_id"], "profile_version": 1, "language": "en"},
            )
            check(
                "Profile evaluation",
                bool(evaluation["strengths"])
                and evaluation["completeness"]["score"] > 0
                and all(
                    a["basis"] in {"missing_field", "market_gap", "wording"}
                    for a in evaluation["advice"]
                ),
            )

            async def chat(
                message, revision, *, target=conversation, user="demo-candidate", **more
            ):
                body = {
                    "conversation_id": target,
                    "expected_revision": revision,
                    "message": message,
                    **more,
                }
                return (await post("/agent/chat", body, user=user))["response"]

            changed = await chat("Tunis instead", 1)
            check(
                "Follow-up memory",
                changed["filters"] == {"location": "Tunis", "contract": "PFE", "skills": ["Python"]}
                and changed["results"]
                and {r["company"]["city"] for r in changed["results"]} == {"tunis"},
            )
            empty = await chat("Sousse instead", 2)
            check("No invented matches", empty["status"] == "no_matches" and not empty["results"])
            ambiguous = await chat("Find Python PFE in Sfax or Tunis", 3)
            check(
                "Ambiguous request",
                ambiguous["status"] == "clarification_required" and not ambiguous["results"],
            )
            advice = await chat("Advice on Python skills for my CV", 4)
            check(
                "Advice and RAG",
                advice["status"] == "answered"
                and advice["intent"] == "advice"
                and any(r["cited"] for r in advice["references"]),
            )
            stale = await post(
                "/agent/chat",
                {"conversation_id": conversation, "expected_revision": 0, "message": "Python"},
                expected=409,
            )
            check("Stale conversation", stale["detail"]["code"] == "stale_conversation_revision")
            denied = await client.get(
                f"/api/v1/conversations/{conversation}",
                headers={"X-Demo-User": "demo-candidate-two"},
            )
            check("Candidate isolation", denied.status_code == 404)
            recruiter = await post(
                "/api/v1/conversations", {"language": "en"}, user="demo-recruiter", expected=201
            )
            sourced = await chat(
                "Find ReactJS frontend candidates in Sfax",
                0,
                target=recruiter["id"],
                user="demo-recruiter",
            )
            percents = [r["compatibility"]["percent"] for r in sourced["results"]]
            check(
                "Recruiter sourcing",
                sourced["status"] == "matches_found"
                and sourced["target"] == "candidates"
                and percents == sorted(percents, reverse=True)
                and all("%" in r["explanation"] for r in sourced["results"]),
            )
            hidden = await chat(
                "Find ReactJS frontend candidates in Sfax",
                1,
                target=recruiter["id"],
                user="demo-recruiter",
                anonymized=True,
            )
            check(
                "Anonymized sourcing",
                hidden["anonymized"]
                and hidden["results"]
                and all(
                    r["candidate_ref"].startswith("C-")
                    and r["candidate_id"] is None
                    and r["city"] is None
                    and r["display_name"] == r["candidate_ref"]
                    for r in hidden["results"]
                ),
            )
            posting = await post(
                "/agent/source-candidates",
                {"offer_id": "off-demo-react-pfe", "language": "en"},
                user="demo-recruiter",
            )
            check(
                "Job posting sourcing",
                posting["criteria"] == ["ReactJS", "TypeScript", "REST API", "Git"]
                and posting["offer"]["offer_id"] == "off-demo-react-pfe"
                and bool(posting["results"]),
            )
            application = {"offer_id": "off-demo-react-pfe", "candidate_profile_id": "cand-02"}
            scores = [
                await post("/agent/score-application", application, user="demo-recruiter")
                for _ in range(2)
            ]
            check(
                "Application scoring",
                scores[0] == scores[1] and scores[0]["result"]["explanation_source"] == "template",
            )
            current = result["pack"]["sources"]["offer_version"]
            for offer, version, code in (
                ("off-demo-closed", current, 404),
                ("off-pending-react-pfe", current, 404),
                (result["pack"]["sources"]["offer_id"], "0" * 64, 409),
            ):
                await post(
                    "/agent/generate-pack",
                    {
                        "profile_id": result["profile_id"],
                        "profile_version": 1,
                        "offer_id": offer,
                        "offer_version": version,
                        "language": "en",
                    },
                    expected=code,
                )
            check("Offer eligibility and versions", True)
            response = await client.get("/agent/capabilities")
            response.raise_for_status()
            capabilities = response.json()
            check(
                "Integration gates",
                capabilities["offer_search"] == "platform"
                and capabilities["matching_index_ready"] is True
                and capabilities["submission_enabled"] is False
                and capabilities["profile_sync"] == "not_integrated",
            )
        report["passed"] = True
    except (
        httpx.HTTPError,
        RuntimeError,
        ValueError,
        AssertionError,
        KeyError,
        TimeoutError,
    ) as exc:
        report["checks"].append(
            {
                "scenario": "Run completion",
                "status": "failed",
                "expected": "All scenarios complete",
                "error": str(exc) or type(exc).__name__,
            }
        )
    report["finished_at"] = datetime.now(UTC).isoformat()
    return report
