import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from matching_support import WordEmbedder, demo_settings
from pydantic import ValidationError

from app.api.persistence import get_store
from app.contracts.models import (
    AcceptedProfileSync,
    CandidateRefRequest,
    ChatReceipt,
    ChatRequest,
    EvaluateProfileRequest,
    GeneratePackRequest,
    ScoreApplicationRequest,
    SourceCandidatesRequest,
)
from app.cv.worker import CVWorker
from app.db.store import Store
from app.demo import create_demo_app, simulated_response
from app.domain import ProfileCreate
from app.identity import Principal, get_principal
from app.main import create_app
from app.matching.catalog import PlatformCatalog
from app.matching.seed import SEED, seed_platform
from app.matching.worker import MatchSyncWorker
from app.worker import DraftWorker

DATA = Path(__file__).resolve().parents[1] / "data/contracts"
CANDIDATE = Principal(user_id="demo-candidate", role="CANDIDATE")
SHA = "a" * 64


@pytest.mark.mongodb
async def test_consumer_example_exercises_all_three_facades_with_demo_workers(
    settings, mongo_db, monkeypatch, qdrant_http
):
    from examples.agent_consumer import run

    @asynccontextmanager
    async def connection(config):
        yield mongo_db

    monkeypatch.setattr("app.main.mongo_connection", connection)
    config = settings.model_copy(update={"qdrant_url": os.environ["SID_TEST_QDRANT_URL"]})
    app = create_demo_app(config, live=False, knowledge_embedder=WordEmbedder())
    async with app.router.lifespan_context(app):
        await run("http://127.0.0.1", transport=httpx.ASGITransport(app=app))
    assert await mongo_db.sid_cv_imports.count_documents({"status": "accepted"}) == 1
    assert await mongo_db.sid_drafts.count_documents({"status": "draft"}) == 1
    assert await mongo_db.sid_approvals.count_documents({}) == 0


@pytest.mark.mongodb
async def test_advice_branch_returns_typed_references_through_facade(
    settings, mongo_db, monkeypatch
):
    from test_conversations import Knowledge

    async with facade(settings, mongo_db, monkeypatch, platform=False) as (client, app):
        app.state.knowledge_search = Knowledge()
        conversation = (await client.post("/api/v1/conversations", json={"language": "en"})).json()
        answer = await client.post(
            "/agent/chat",
            json={
                "conversation_id": conversation["id"],
                "expected_revision": 0,
                "message": "Help me improve Python skills",
            },
            headers={"Idempotency-Key": "facade-advice-1"},
        )
        assert answer.status_code == 200
        turn = ChatReceipt.model_validate(answer.json())
        assert turn.contract_version == "agent-v2"
        assert turn.response.status == "answered" and turn.response.references[0].cited
        assert turn.response.knowledge_generation == "test-generation"
        assert turn.response.search_mode == "disabled" and turn.response.results == []


def test_facade_openapi_has_typed_requests_outputs_errors_and_combined_auth(settings):
    schema = create_app(settings).openapi()
    for path in ("parse-cv", "generate-pack", "chat"):
        operation = schema["paths"][f"/agent/{path}"]["post"]
        status = "200" if path == "chat" else "202"
        assert "$ref" in operation["responses"][status]["content"]["application/json"]["schema"]
        assert operation["security"] == [{"APIKeyHeader": [], "PlatformUserToken": []}]
        assert "anyOf" in operation["responses"]["422"]["content"]["application/json"]["schema"]
        assert any(p["name"].casefold() == "idempotency-key" for p in operation["parameters"])
    assert "application/pdf" in schema["paths"]["/agent/parse-cv"]["post"]["requestBody"]["content"]
    pack = schema["components"]["schemas"]["GeneratePackRequest"]["properties"]["offer_version"]
    assert pack["type"] == "string" and pack["pattern"]
    for path in (
        "/api/v1/tasks/{task_id}",
        "/api/v1/cv-imports/{import_id}",
        "/api/v1/application-drafts/{draft_id}",
    ):
        assert (
            "$ref"
            in schema["paths"][path]["get"]["responses"]["200"]["content"]["application/json"][
                "schema"
            ]
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("role", "RECRUITER"),
        ("job_description", "fake job"),
        ("offer_version", 1),
        ("offer_version", "not-a-hash"),
        ("profile_version", 0),
    ],
)
def test_pack_contract_rejects_client_role_text_and_invalid_versions(field, value):
    payload = {
        "profile_id": "profile",
        "profile_version": 1,
        "offer_id": "offer",
        "offer_version": SHA,
    }
    GeneratePackRequest.model_validate(payload)
    with pytest.raises(ValidationError):
        GeneratePackRequest.model_validate({**payload, field: value})


def test_profile_sync_contract_needs_exact_acceptance_and_boolean_confirmation():
    payload = json.loads((DATA / "requests-v2.json").read_text("utf-8"))["accepted_profile_sync"]
    assert AcceptedProfileSync.model_validate(payload).confirmed is True
    for value in (False, 1, "true"):
        with pytest.raises(ValidationError):
            AcceptedProfileSync.model_validate({**payload, "confirmed": value})


def test_every_published_request_example_validates():
    examples = json.loads((DATA / "requests-v2.json").read_text("utf-8"))
    models = {
        "generate_pack": GeneratePackRequest,
        "chat": ChatRequest,
        "chat_recruiter_anonymized": ChatRequest,
        "source_candidates_offer": SourceCandidatesRequest,
        "source_candidates_text": SourceCandidatesRequest,
        "score_application": ScoreApplicationRequest,
        "evaluate_profile": EvaluateProfileRequest,
        "resolve_candidate_ref": CandidateRefRequest,
        "accepted_profile_sync": AcceptedProfileSync,
    }
    assert examples["contract_version"] == "agent-v2"
    assert set(models) == set(examples) - {"contract_version", "note"}
    for name, model in models.items():
        model.model_validate(examples[name])


async def test_default_facade_fails_closed_without_identity_or_platform_data(settings):
    app = create_app(settings)
    payload = {
        "profile_id": "profile",
        "profile_version": 1,
        "offer_id": "offer",
        "offer_version": SHA,
    }
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.post("/agent/generate-pack", json=payload)).status_code == 401
            client.headers["X-Service-Key"] = "test-service-key"
            blocked = await client.post("/agent/generate-pack", json=payload)
            assert blocked.json()["detail"]["code"] == "platform_identity_not_integrated"
            caps = (await client.get("/agent/capabilities")).json()
            assert caps["contract_version"] == "agent-v2"
            assert caps["offer_search"] == "disabled" and caps["candidate_sourcing"] == "disabled"
            assert caps["matching_index_ready"] is False and caps["submission_enabled"] is False
            app.dependency_overrides[get_principal] = lambda: CANDIDATE
            app.dependency_overrides[get_store] = lambda: SimpleNamespace()
            blocked = await client.post(
                "/agent/generate-pack", json=payload, headers={"Idempotency-Key": "fixture-pack-1"}
            )
            assert blocked.json()["detail"]["code"] == "platform_data_unavailable"


async def test_parse_facade_retains_pdf_body_allowance_and_size_cap(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            medium = b"%PDF-1.7\n" + b"x" * (settings.http_json_max_bytes + 1)
            # It reaches auth, rather than mistakenly receiving the smaller JSON cap.
            assert (await client.post("/agent/parse-cv", content=medium)).status_code == 401
            too_large = await client.post("/agent/parse-cv", content=b"x" * (5 * 1024 * 1024 + 1))
            assert too_large.status_code == 413


TOKENS = {
    "Bearer fixture-candidate": "demo-candidate",
    "Bearer fixture-other": "demo-candidate-two",
    "Bearer fixture-recruiter": "demo-recruiter",
    "Bearer fixture-recruiter-two": "rec-atlas",
}


@asynccontextmanager
async def facade(
    settings, db, monkeypatch, *, handler=simulated_response, platform=True, sync=True
):
    @asynccontextmanager
    async def connection(config):
        yield db

    async def verify(request):
        token = request.headers.get("Authorization", "")
        if token not in TOKENS:
            raise HTTPException(401, detail={"code": "fixture_token_invalid"})
        return TOKENS[token]

    config = demo_settings(settings)
    if platform:
        url = os.environ.get("SID_TEST_QDRANT_URL")
        if not url:
            pytest.skip("Set SID_TEST_QDRANT_URL to opt into platform matching tests")
        config = config.model_copy(update={"qdrant_url": url})
    await seed_platform(db, config)
    embedder = WordEmbedder()
    monkeypatch.setattr("app.main.mongo_connection", connection)
    app = create_app(
        config,
        verify_user_id=verify,
        transport=httpx.MockTransport(handler),
        knowledge_embedder=embedder,
    )
    async with app.router.lifespan_context(app):
        if platform and sync:
            qdrant = app.state.match_engine.index.qdrant
            await MatchSyncWorker(db, qdrant, embedder, config).run_once()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={
                    "X-Service-Key": "test-service-key",
                    "Authorization": "Bearer fixture-candidate",
                },
            ) as client:
                yield client, app
        finally:
            if platform:
                for kind in ("offers", "candidates"):
                    state = await db.sid_match_state.find_one({"_id": kind})
                    if state and state.get("active"):
                        await app.state.match_engine.index.qdrant.drop(state["active"])


async def accepted_profile(db, content="skill: Python\nskill: Django\nskill: Git\n"):
    store = Store(db)
    profile = await store.create_profile(
        CANDIDATE,
        ProfileCreate(content=content + "project: Application web avec Django et PostgreSQL"),
    )
    await store.accept_profile(CANDIDATE, profile["_id"], 1)
    return profile["_id"]


async def chat(client, conversation_id, revision, message, key, **extra):
    headers = extra.pop("headers", {})
    return await client.post(
        "/agent/chat",
        json={
            "conversation_id": conversation_id,
            "expected_revision": revision,
            "message": message,
            **extra,
        },
        headers={"Idempotency-Key": key, **headers},
    )


@pytest.mark.mongodb
async def test_pack_facade_uses_platform_offer_versions_and_rechecks_approval(
    settings, mongo_db, monkeypatch
):
    async with facade(settings, mongo_db, monkeypatch) as (client, app):
        profile_id = await accepted_profile(mongo_db)
        catalog = PlatformCatalog(mongo_db, app.state.settings)
        offer = await catalog.offer("off-demo-python-pfe")
        payload = {
            "profile_id": profile_id,
            "profile_version": 1,
            "offer_id": offer.id,
            "offer_version": offer.version,
        }
        headers = {"Idempotency-Key": "platform-pack-1"}
        first = await client.post("/agent/generate-pack", json=payload, headers=headers)
        assert first.status_code == 202
        receipt = first.json()
        assert first.headers["Location"] == receipt["poll_url"]
        assert receipt["sources"]["kind"] == "platform_offer" and not receipt["submitted"]
        assert receipt["sources"]["offer_version"] == offer.version
        assert (
            await client.post("/agent/generate-pack", json=payload, headers=headers)
        ).json() == receipt
        other = await catalog.offer("off-demo-react-pfe")
        conflict = await client.post(
            "/agent/generate-pack",
            json={**payload, "offer_id": other.id, "offer_version": other.version},
            headers=headers,
        )
        assert conflict.status_code == 409
        stale = await client.post(
            "/agent/generate-pack",
            json={**payload, "offer_version": SHA},
            headers={"Idempotency-Key": "platform-pack-2"},
        )
        assert stale.json()["detail"]["code"] == "stale_offer_version"
        closed = await client.post(
            "/agent/generate-pack",
            json={**payload, "offer_id": "off-demo-closed"},
            headers={"Idempotency-Key": "platform-pack-3"},
        )
        assert closed.status_code == 404
        assert await DraftWorker(mongo_db, app.state.ai_gateway, app.state.settings).run_once()
        assert (await client.get(receipt["poll_url"])).json()["state"] == "done"
        draft = (await client.get(receipt["draft_url"])).json()
        assert draft["source_snapshot"]["kind"] == "platform_offer"
        assert draft["source_snapshot"]["offer"] == receipt["sources"]
        assert offer.description in draft["job_description"]
        assert (
            "Atlas" not in draft["company_context"] and "Demo software" in draft["company_context"]
        )
        await mongo_db.demo_offers.update_one(
            {"id": offer.id}, {"$set": {"description": offer.description + " Docker apprécié."}}
        )
        approval = await client.post(
            f"/api/v1/application-drafts/{receipt['draft_id']}/approvals",
            json={
                "expected_version": draft["version"],
                "confirmed": True,
                "destination_id": offer.id,
            },
        )
        assert approval.status_code == 409
        assert approval.json()["detail"]["code"] == "offer_changed_since_generation"


@pytest.mark.mongodb
async def test_parse_receipt_replay_review_and_isolation(settings, mongo_db, monkeypatch):
    from test_cv import pdf

    async with facade(settings, mongo_db, monkeypatch, platform=False) as (client, app):
        headers = {"Content-Type": "application/pdf", "Idempotency-Key": "fixture-cv-001"}
        response = await client.post(
            "/agent/parse-cv",
            content=pdf(["Name: Synthetic Candidate", "Skill: Python"]),
            headers=headers,
        )
        assert response.status_code == 202
        receipt = response.json()
        assert receipt["sync_status"] == "not_integrated" and receipt["requires_human_approval"]
        assert await CVWorker(mongo_db, app.state.ai_gateway, app.state.settings).run_once()
        review = await client.get(receipt["poll_url"])
        assert review.status_code == 200 and review.json()["status"] == "review"
        assert review.json()["facts"] and review.json()["evaluation"]["kind"] == "completeness"
        assert (
            await client.get(receipt["poll_url"], headers={"Authorization": "Bearer fixture-other"})
        ).status_code == 404
        replay = await client.post(
            "/agent/parse-cv",
            content=pdf(["Name: Synthetic Candidate", "Skill: Python"]),
            headers=headers,
        )
        assert replay.status_code == 202 and replay.json()["import_id"] == receipt["import_id"]
        assert replay.json()["status"] == "review"


@pytest.mark.mongodb
async def test_candidate_chat_searches_platform_offers_with_followup_and_replay(
    settings, mongo_db, monkeypatch
):
    async with facade(settings, mongo_db, monkeypatch) as (client, app):
        profile_id = await accepted_profile(mongo_db)
        conversation = (
            await client.post(
                "/api/v1/conversations",
                json={"language": "fr", "profile_id": profile_id, "profile_version": 1},
            )
        ).json()
        first = await chat(
            client,
            conversation["id"],
            0,
            "Je cherche un stage PFE Python à Sfax",
            "candidate-chat-1",
        )
        assert first.status_code == 200, first.text
        turn = ChatReceipt.model_validate(first.json())
        response = turn.response
        assert response.status == "matches_found" and response.search_mode == "platform"
        assert response.target == "offers" and not turn.submitted
        assert 1 <= len(response.results) <= 5
        for offer in response.results:
            assert offer.contract == "PFE" and offer.company.city == "sfax"
            assert offer.compatibility is not None and offer.explanation
            assert offer.explanation_source == "model"
        assert response.results[0].id == "off-demo-python-pfe"
        replay = await chat(
            client,
            conversation["id"],
            0,
            "Je cherche un stage PFE Python à Sfax",
            "candidate-chat-1",
        )
        assert replay.json() == first.json()
        assert await mongo_db.sid_conversation_turns.count_documents({}) == 1
        changed = await chat(client, conversation["id"], 1, "Tunis instead", "candidate-chat-2")
        assert changed.status_code == 200
        moved = changed.json()["response"]
        assert moved["filters"]["contract"] == "PFE" and moved["filters"]["location"] == "Tunis"
        assert {r["company"]["city"] for r in moved["results"]} == {"tunis"}
        other = await chat(
            client,
            conversation["id"],
            2,
            "Python",
            "candidate-chat-3",
            headers={"Authorization": "Bearer fixture-other"},
        )
        assert other.status_code == 404
        rejected = await chat(
            client, conversation["id"], 2, "Find Python", "candidate-chat-4", anonymized=True
        )
        assert rejected.status_code == 422
        assert rejected.json()["detail"]["code"] == "anonymization_recruiter_only"


@pytest.mark.mongodb
async def test_recruiter_chat_sources_candidates_by_compatibility_and_anonymizes(
    settings, mongo_db, monkeypatch
):
    recruiter = {"Authorization": "Bearer fixture-recruiter"}
    async with facade(settings, mongo_db, monkeypatch) as (client, app):
        conversation = (
            await client.post("/api/v1/conversations", json={"language": "en"}, headers=recruiter)
        ).json()
        named = await chat(
            client,
            conversation["id"],
            0,
            "Find ReactJS frontend candidates in Sfax",
            "recruiter-1",
            headers=recruiter,
        )
        assert named.status_code == 200
        response = named.json()["response"]
        assert response["status"] == "matches_found" and response["target"] == "candidates"
        percents = [r["compatibility"]["percent"] for r in response["results"]]
        assert percents == sorted(percents, reverse=True) and percents[0] > 0
        assert all(r["candidate_id"] and not r["anonymized"] for r in response["results"])
        anonymous = await chat(
            client,
            conversation["id"],
            1,
            "Find ReactJS frontend candidates in Sfax",
            "recruiter-2",
            headers=recruiter,
            anonymized=True,
        )
        hidden = anonymous.json()["response"]
        assert hidden["anonymized"] is True and hidden["results"]
        blob = json.dumps(hidden)
        for person in json.loads(SEED.read_text("utf-8"))["candidates"]:
            assert person["fullName"] not in blob and person["phone"] not in blob
        assert all(r["candidate_ref"].startswith("C-") for r in hidden["results"])


@pytest.mark.mongodb
async def test_replay_revalidates_results(settings, mongo_db, monkeypatch):
    async with facade(settings, mongo_db, monkeypatch) as (client, app):
        conversation = (await client.post("/api/v1/conversations", json={"language": "en"})).json()
        first = await chat(client, conversation["id"], 0, "Find Flutter mobile PFE", "replay-1")
        assert first.status_code == 200
        returned = first.json()["response"]["results"][0]["id"]
        await mongo_db.demo_offers.update_one({"id": returned}, {"$set": {"title": "Renamed"}})
        replay = await chat(client, conversation["id"], 0, "Find Flutter mobile PFE", "replay-1")
        assert replay.status_code == 409
        assert replay.json()["detail"]["code"] == "search_results_changed"
        assert await mongo_db.sid_conversation_turns.count_documents({}) == 1


@pytest.mark.mongodb
async def test_offer_changed_during_generation_cannot_commit(settings, mongo_db, monkeypatch):
    def handler(request):
        data = json.loads(json.loads(request.content)["messages"][1]["content"])
        if data.get("schema", {}).get("title") == "MatchAnswer":
            for match in data["matches"]:
                changed.append(match["id"])
        return simulated_response(request)

    changed = []
    async with facade(settings, mongo_db, monkeypatch, handler=handler) as (client, app):
        original = app.state.ai_gateway.generate

        async def generate(*args, **kwargs):
            result = await original(*args, **kwargs)
            if changed:
                await mongo_db.demo_offers.update_one(
                    {"id": changed[0]}, {"$set": {"title": "Edited during generation"}}
                )
            return result

        app.state.ai_gateway.generate = generate
        conversation = (await client.post("/api/v1/conversations", json={"language": "en"})).json()
        response = await chat(client, conversation["id"], 0, "Find Python PFE Sfax", "during-1")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "search_results_changed"
        assert await mongo_db.sid_conversation_turns.count_documents({}) == 0
        head = await mongo_db.sid_conversations.find_one({"_id": conversation["id"]})
        assert head["revision"] == 0


@pytest.mark.mongodb
@pytest.mark.parametrize(
    "evidence",
    [
        {"id": "not-returned", "quote": "fabricated", "reason": "Invented"},
        {"id": "off-demo-python-pfe", "quote": "fabricated quote", "reason": "Bad quote"},
        {"id": "off-demo-python-pfe", "quote": "Python", "reason": "Matching 99% invented"},
    ],
)
async def test_invalid_explanation_falls_back_to_template(
    settings, mongo_db, monkeypatch, evidence
):
    def invalid(request):
        if request.url.host == "generativelanguage.googleapis.com":
            return httpx.Response(503)
        data = json.loads(json.loads(request.content)["messages"][1]["content"])
        if data.get("schema", {}).get("title") == "MatchAnswer":
            content = json.dumps({"text": "Invented match", "evidence": [evidence]})
            return httpx.Response(
                200,
                json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]},
            )
        return simulated_response(request)

    async with facade(settings, mongo_db, monkeypatch, handler=invalid) as (client, app):
        profile_id = await accepted_profile(mongo_db)
        conversation = (
            await client.post(
                "/api/v1/conversations",
                json={"language": "en", "profile_id": profile_id, "profile_version": 1},
            )
        ).json()
        answered = await chat(client, conversation["id"], 0, "Find Python PFE Sfax", "invalid-1")
        assert answered.status_code == 200
        response = answered.json()["response"]
        assert response["explanation_source"] == "template" and response["generation"] is None
        for result in response["results"]:
            assert result["explanation_source"] == "template"
            assert result["explanation"].startswith(
                f"Matching {result['compatibility']['percent']}%"
            )
            assert "99%" not in json.dumps(response)


@pytest.mark.mongodb
async def test_no_results_and_ambiguous_filters_have_explicit_statuses(
    settings, mongo_db, monkeypatch
):
    async with facade(settings, mongo_db, monkeypatch) as (client, app):
        conversation = (await client.post("/api/v1/conversations", json={"language": "en"})).json()
        empty = await chat(client, conversation["id"], 0, "Find Python PFE Sousse", "no-matches-1")
        assert empty.status_code == 200 and empty.json()["response"]["status"] == "no_matches"
        assert empty.json()["response"]["generation"] is None
        ambiguous = await chat(
            client, conversation["id"], 1, "Find Python PFE in Sfax or Tunis", "ambiguous-1"
        )
        assert ambiguous.json()["response"]["status"] == "clarification_required"
        assert ambiguous.json()["response"]["results"] == []


@pytest.mark.mongodb
async def test_index_not_ready_is_503(settings, mongo_db, monkeypatch):
    async with facade(settings, mongo_db, monkeypatch, sync=False) as (client, app):
        conversation = (await client.post("/api/v1/conversations", json={"language": "en"})).json()
        response = await chat(client, conversation["id"], 0, "Find Python PFE Sfax", "unready-1")
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "matching_index_not_ready"
        assert await mongo_db.sid_conversation_turns.count_documents({}) == 0


def test_pack_job_text_uses_language_punctuation():
    from app.api.agent import pack_job_text

    offer = SimpleNamespace(title="PFE", description="Build things.", requirements=["Python"])
    assert pack_job_text(offer, "en").endswith("Required skills: Python")
    assert pack_job_text(offer, "fr").endswith("Compétences requises : Python")
