"""Company branch: sourcing, application scoring, anonymization and index status."""

import json

import pytest
from test_agent_contract import facade

from app.demo import simulated_response
from app.matching.catalog import PlatformCatalog
from app.matching.seed import SEED

RECRUITER = {"Authorization": "Bearer fixture-recruiter"}
OTHER_RECRUITER = {"Authorization": "Bearer fixture-recruiter-two"}
SEEDED = json.loads(SEED.read_text("utf-8"))["candidates"]


def assert_no_identity(blob):
    for person in SEEDED:
        assert person["fullName"] not in blob
        assert person["phone"] not in blob
    assert "@example.test" not in blob


@pytest.mark.mongodb
async def test_source_candidates_for_own_offer_ranks_by_compatibility(
    settings, mongo_db, monkeypatch
):
    async with facade(settings, mongo_db, monkeypatch) as (client, app):
        response = await client.post(
            "/agent/source-candidates",
            json={"offer_id": "off-demo-react-pfe", "language": "en"},
            headers=RECRUITER,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["contract_version"] == "agent-v2" and body["submitted"] is False
        assert body["criteria"] == ["ReactJS", "TypeScript", "REST API", "Git"]
        offer = await PlatformCatalog(mongo_db, app.state.settings).offer("off-demo-react-pfe")
        assert body["offer"]["offer_version"] == offer.version
        percents = [r["compatibility"]["percent"] for r in body["results"]]
        assert percents and percents == sorted(percents, reverse=True)
        assert all(f"{r['compatibility']['percent']}%" in r["explanation"] for r in body["results"])


@pytest.mark.mongodb
async def test_source_candidates_from_job_description_extracts_reviewed_skills(
    settings, mongo_db, monkeypatch
):
    async with facade(settings, mongo_db, monkeypatch) as (client, app):
        response = await client.post(
            "/agent/source-candidates",
            json={
                "job_description": "Junior developer with ReactJS, Node.js and MongoDB in Sfax.",
                "language": "fr",
            },
            headers=RECRUITER,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["criteria"] == ["React", "Node.js", "MongoDB"] and body["offer"] is None
        assert body["results"] and body["results"][0]["compatibility"]["percent"] >= 60


@pytest.mark.mongodb
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"offer_id": "off-demo-react-pfe", "job_description": "A long enough job description."},
    ],
)
async def test_source_candidates_needs_exactly_one_source(settings, mongo_db, monkeypatch, payload):
    async with facade(settings, mongo_db, monkeypatch) as (client, app):
        response = await client.post("/agent/source-candidates", json=payload, headers=RECRUITER)
        assert response.status_code == 422


@pytest.mark.mongodb
async def test_sourcing_is_company_scoped_and_recruiter_only(settings, mongo_db, monkeypatch):
    async with facade(settings, mongo_db, monkeypatch) as (client, app):
        foreign = await client.post(
            "/agent/source-candidates", json={"offer_id": "off-atlas-php-job"}, headers=RECRUITER
        )
        assert foreign.status_code == 404
        assert foreign.json()["detail"]["code"] == "offer_not_accessible"
        candidate = await client.post(
            "/agent/source-candidates", json={"offer_id": "off-demo-react-pfe"}
        )
        assert candidate.status_code == 403
        assert candidate.json()["detail"]["code"] == "recruiter_access_required"


@pytest.mark.mongodb
async def test_score_application_is_deterministic_and_pins_offer(settings, mongo_db, monkeypatch):
    async with facade(settings, mongo_db, monkeypatch) as (client, app):
        payload = {"offer_id": "off-demo-react-pfe", "candidate_profile_id": "cand-02"}
        first = await client.post("/agent/score-application", json=payload, headers=RECRUITER)
        second = await client.post("/agent/score-application", json=payload, headers=RECRUITER)
        assert first.status_code == 200, first.text
        assert first.json() == second.json()
        body = first.json()
        result = body["result"]
        assert result["relevance"] is None and result["explanation_source"] == "template"
        assert result["explanation"].startswith(f"Matching {result['compatibility']['percent']} %")
        assert body["offer"]["offer_id"] == "off-demo-react-pfe"
        missing = await client.post(
            "/agent/score-application",
            json={**payload, "candidate_profile_id": "cand-40"},
            headers=RECRUITER,
        )
        assert missing.json()["detail"]["code"] == "candidate_not_accessible"


@pytest.mark.mongodb
async def test_anonymized_prompt_and_response_have_no_identity(settings, mongo_db, monkeypatch):
    prompts = []

    def capture(request):
        prompts.append(request.content.decode())
        return simulated_response(request)

    async with facade(settings, mongo_db, monkeypatch, handler=capture) as (client, app):
        response = await client.post(
            "/agent/source-candidates",
            json={"offer_id": "off-demo-fullstack-job", "anonymized": True, "language": "en"},
            headers=RECRUITER,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["anonymized"] and body["results"]
        assert prompts, "the explanation step must have called the model"
        assert_no_identity(json.dumps(body))
        assert_no_identity(json.dumps(prompts))
        assert all(r["city"] is None and r["candidate_id"] is None for r in body["results"])


@pytest.mark.mongodb
async def test_candidate_refs_resolve_only_for_issuing_company(settings, mongo_db, monkeypatch):
    async with facade(settings, mongo_db, monkeypatch) as (client, app):
        sourced = await client.post(
            "/agent/source-candidates",
            json={"offer_id": "off-demo-react-pfe", "anonymized": True},
            headers=RECRUITER,
        )
        reference = sourced.json()["results"][0]["candidate_ref"]
        resolved = await client.post(
            "/agent/candidate-refs/resolve", json={"candidate_ref": reference}, headers=RECRUITER
        )
        assert resolved.status_code == 200
        assert resolved.json()["candidate_profile_id"].startswith("cand-")
        foreign = await client.post(
            "/agent/candidate-refs/resolve",
            json={"candidate_ref": reference},
            headers=OTHER_RECRUITER,
        )
        assert foreign.status_code == 404


@pytest.mark.mongodb
async def test_matching_status_and_capabilities_report_ready_indexes(
    settings, mongo_db, monkeypatch
):
    async with facade(settings, mongo_db, monkeypatch) as (client, app):
        status = await client.get("/api/v1/matching/status")
        assert status.status_code == 200
        body = status.json()
        assert body["configured"] is True
        for kind in ("offers", "candidates"):
            assert body[kind]["ready"] and body[kind]["records"] > 0
        assert (
            await client.get("/api/v1/matching/status", headers={"X-Service-Key": "wrong"})
        ).status_code == 401
        caps = (await client.get("/agent/capabilities")).json()
        assert (
            caps["candidate_sourcing"] == "platform" and caps["application_scoring"] == "platform"
        )
        assert caps["matching_index_ready"] is True and caps["anonymization"] is True
