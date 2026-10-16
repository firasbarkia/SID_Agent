"""§5.1 profile evaluator: completeness, grounded strengths, concrete advice, market gaps."""

import json

import httpx
import pytest
from test_agent_contract import accepted_profile, facade

from app.db.store import Store
from app.demo import simulated_response
from app.domain import ProfileEdit
from app.identity import Principal

CANDIDATE = Principal(user_id="demo-candidate", role="CANDIDATE")
PROFILE = (
    "name: Sami Exemple\nemail: sami.exemple@example.test\nphone: +216 00 000 999\n"
    "skill: Python\nskill: Django\nskill: Git\n"
)


async def evaluate(client, profile_id, version=1, **headers):
    return await client.post(
        "/agent/evaluate-profile",
        json={"profile_id": profile_id, "profile_version": version, "language": "en"},
        headers=headers,
    )


@pytest.mark.mongodb
async def test_evaluation_has_grounded_strengths_market_gaps_and_cache(
    settings, mongo_db, monkeypatch
):
    prompts = []

    def capture(request):
        prompts.append(request.content.decode())
        return simulated_response(request)

    async with facade(settings, mongo_db, monkeypatch, handler=capture) as (client, app):
        profile_id = await accepted_profile(mongo_db, PROFILE)
        first = await evaluate(client, profile_id)
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["contract_version"] == "agent-v2" and body["cached"] is False
        assert body["assessment_source"] == "model" and body["generation"]
        assert body["completeness"]["kind"] == "completeness" and body["completeness"]["score"] > 0
        profile_text = (await Store(mongo_db).profile(CANDIDATE, profile_id))["content"]
        assert body["strengths"]
        assert all(s["quote"] in profile_text for s in body["strengths"])
        assert {a["basis"] for a in body["advice"]} <= {"missing_field", "market_gap", "wording"}
        gaps = body["market_gaps"]
        assert gaps and all(0 < g["offers"] <= g["of"] <= 12 for g in gaps)
        assert all(g["requirement"] not in {"Python", "Django", "Git"} for g in gaps)
        blob = json.dumps(prompts)
        assert "Sami" not in blob and "example.test" not in blob and "000 999" not in blob
        calls = len(prompts)
        again = await evaluate(client, profile_id)
        assert again.json()["cached"] is True and len(prompts) == calls
        assert {k: v for k, v in again.json().items() if k != "cached"} == {
            k: v for k, v in body.items() if k != "cached"
        }


@pytest.mark.mongodb
async def test_invalid_strength_quote_falls_back_to_template(settings, mongo_db, monkeypatch):
    def invalid(request):
        if request.url.host == "generativelanguage.googleapis.com":
            return httpx.Response(503)
        data = json.loads(json.loads(request.content)["messages"][1]["content"])
        if data.get("schema", {}).get("title") == "AssessmentAnswer":
            content = json.dumps(
                {
                    "strengths": [{"text": "Expert in Kubernetes", "quote": "Kubernetes expert"}],
                    "advice": [{"text": "Learn Rust", "basis": "market_gap"}],
                }
            )
            return httpx.Response(
                200,
                json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]},
            )
        return simulated_response(request)

    async with facade(settings, mongo_db, monkeypatch, handler=invalid) as (client, app):
        profile_id = await accepted_profile(mongo_db, PROFILE)
        response = await evaluate(client, profile_id)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["assessment_source"] == "template" and body["generation"] is None
        assert "Kubernetes" not in json.dumps(body["strengths"])
        market = [a for a in body["advice"] if a["basis"] == "market_gap"]
        assert market and all(
            any(g["requirement"] in a["text"] for g in body["market_gaps"]) for a in market
        )


@pytest.mark.mongodb
async def test_evaluation_requires_the_current_validated_version(settings, mongo_db, monkeypatch):
    async with facade(settings, mongo_db, monkeypatch) as (client, app):
        profile_id = await accepted_profile(mongo_db, PROFILE)
        await Store(mongo_db).edit_profile(
            CANDIDATE,
            profile_id,
            ProfileEdit(expected_version=1, content=PROFILE + "skill: Docker\n"),
        )
        for version in (1, 2):
            response = await evaluate(client, profile_id, version)
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == "profile_version_not_validated"


@pytest.mark.mongodb
async def test_evaluation_is_private_to_the_candidate(settings, mongo_db, monkeypatch):
    async with facade(settings, mongo_db, monkeypatch) as (client, app):
        profile_id = await accepted_profile(mongo_db, PROFILE)
        other = await evaluate(client, profile_id, Authorization="Bearer fixture-other")
        assert other.status_code == 404
        recruiter = await evaluate(client, profile_id, Authorization="Bearer fixture-recruiter")
        assert recruiter.status_code == 403
        assert recruiter.json()["detail"]["code"] == "candidate_access_required"
