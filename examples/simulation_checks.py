"""Behavior scenarios shared by the simulation console and command-line runner."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from app.demo import DEMO_SERVICE_KEY
from examples.agent_consumer import run


async def run_checks(base_url, *, transport=None):
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "mode": "synthetic",
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

    def check(scenario, passed, expected):
        report["checks"].append(
            {
                "scenario": scenario,
                "status": "passed" if passed else "failed",
                "expected": expected,
            }
        )
        if not passed:
            raise RuntimeError(f"{scenario}: expected {expected}")

    try:
        # Reuse the actual consumer flow: PDF -> review -> accept -> chat -> pack.
        result = await run(
            base_url, transport=transport, event_hooks={"response": [trace]}, verbose=False
        )
        report["records"] = {
            name: result[name]
            for name in ("profile_id", "conversation_id", "import_id", "draft_id")
        }
        for name, expected in (
            ("CV import and review", "Real PDF worker reaches review before synthetic acceptance"),
            ("Candidate offer search", "Only fixture-python-pfe for Python PFE in Sfax"),
            ("Idempotent retry", "Same key and payload return the same saved turn"),
            ("Pack generation", "Worker finishes; human approval required; submitted=false"),
        ):
            check(name, True, expected)

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

            async def chat(message, revision):
                return await post(
                    "/agent/chat",
                    {
                        "conversation_id": conversation,
                        "expected_revision": revision,
                        "message": message,
                    },
                )

            changed = (await chat("Tunis instead", 1))["response"]
            check(
                "Follow-up memory",
                [r["id"] for r in changed["results"]] == ["fixture-tunis-pfe"]
                and changed["filters"]
                == {"location": "Tunis", "contract": "PFE", "skills": ["Python"]},
                "Location becomes Tunis; Python and PFE remain active",
            )
            empty = (await chat("Sousse instead", 2))["response"]
            check(
                "No invented matches",
                empty["status"] == "no_matches" and not empty["results"],
                "Sousse returns no_matches with an empty result list",
            )
            ambiguous = (await chat("Find Python PFE in Sfax or Tunis", 3))["response"]
            check(
                "Ambiguous request",
                ambiguous["status"] == "clarification_required" and not ambiguous["results"],
                "Conflicting locations require clarification",
            )
            advice = (await chat("Advice on Python skills for my CV", 4))["response"]
            check(
                "Advice and RAG",
                advice["status"] == "answered"
                and advice["intent"] == "advice"
                and any(r["cited"] for r in advice["references"]),
                "Advice branch retrieves local references and cites at least one",
            )
            stale = await post(
                "/agent/chat",
                {
                    "conversation_id": conversation,
                    "expected_revision": 0,
                    "message": "Find Python PFE Sfax",
                },
                expected=409,
            )
            check(
                "Stale conversation",
                stale["detail"]["code"] == "stale_conversation_revision",
                "A stale revision is rejected with HTTP 409",
            )
            denied = await client.get(
                f"/api/v1/conversations/{conversation}",
                headers={"X-Demo-User": "demo-candidate-two"},
            )
            check(
                "Candidate isolation",
                denied.status_code == 404,
                "Candidate two cannot read candidate one's conversation",
            )
            recruiter = await post(
                "/api/v1/conversations", {"language": "en"}, user="demo-recruiter", expected=201
            )
            gated = await post(
                "/agent/chat",
                {
                    "conversation_id": recruiter["id"],
                    "expected_revision": 0,
                    "message": "Find Python candidates",
                },
                user="demo-recruiter",
            )
            check(
                "Recruiter gate",
                gated["response"]["status"] == "matching_unavailable"
                and not gated["response"]["results"],
                "Private candidate discovery stays disabled",
            )
            for offer, version, code in (
                ("fixture-private", 1, 404),
                ("fixture-closed", 2, 404),
                ("fixture-python-pfe", 99, 409),
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
            check(
                "Offer eligibility and versions",
                True,
                "Private/closed offers return 404; stale offer versions return 409",
            )
            response = await client.get("/agent/capabilities")
            response.raise_for_status()
            capabilities = response.json()
            check(
                "Integration gates",
                capabilities["real_private_matching_enabled"] is False
                and capabilities["submission_enabled"] is False
                and capabilities["profile_sync"] == "not_integrated",
                "Real matching, submission and profile sync are not enabled",
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
