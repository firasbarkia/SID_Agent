import asyncio
import hashlib
import json

import httpx
import pytest
from test_persistence import accepted_profile, draft_request

from app.ai.gateway import AIGateway
from app.ai.providers import Generation
from app.api.persistence import get_store
from app.db.store import Store
from app.domain import ApproveDraft, DomainError, DraftEdit, ProfileEdit
from app.dossier import EvidenceEdit, validate_dossier
from app.identity import Principal, get_principal
from app.main import create_app
from app.worker import DraftWorker


def dossier():
    return {
        "cover_letter": "I would like to apply my Python project experience to this role.",
        "evidence": [
            {
                "requirement": "Python",
                "job_quote": "requiring Python",
                "candidate_quote": "Python university project",
                "assessment": "supported",
            }
        ],
        "cv_suggestions": [
            {
                "suggestion": "Highlight your Python university project.",
                "candidate_quote": "Python university project",
            }
        ],
    }


class Provider:
    configured = True
    model = "synthetic-model"

    def __init__(self, name, result, action=None):
        self.name, self.result, self.action = name, result, action

    async def generate(self, system, prompt, max_tokens):
        if self.action:
            await self.action()
        return Generation(json.dumps(self.result), self.name, self.model)


@pytest.mark.parametrize(
    "case",
    [
        "invented_job",
        "invented_candidate",
        "missing_quote",
        "invented_suggestion",
        "word_boundary",
        "false_missing",
    ],
)
def test_rejects_unsupported_quotes(case):
    data = dossier()
    row = data["evidence"][0]
    if case == "invented_job":
        row["job_quote"] = "Python and Kubernetes"
    elif case == "invented_candidate":
        row["candidate_quote"] = "professional Python experience"
    elif case == "missing_quote":
        row["candidate_quote"] = None
    elif case == "invented_suggestion":
        data["cv_suggestions"][0]["candidate_quote"] = "AWS certification"
    elif case == "word_boundary":
        row["requirement"] = "Pyth"
    else:
        row["assessment"] = "not_evidenced"
    with pytest.raises(ValueError):
        validate_dossier(
            json.dumps(data), "Python university project", "Internship requiring Python"
        )


def test_missing_skill_is_reported_without_claiming_evidence():
    data = dossier()
    data["evidence"][0].update(assessment="not_evidenced", candidate_quote=None)
    data["cv_suggestions"] = []
    result = validate_dossier(json.dumps(data), "A student profile", "Internship requiring Python")
    assert result.evidence[0].candidate_quote is None


@pytest.mark.mongodb
async def test_dossier_fallback_snapshots_approval_and_edit(mongo_db, candidate_user, settings):
    store = Store(mongo_db)
    profile = await accepted_profile(store, candidate_user)
    payload = draft_request(profile)
    created = await store.request_draft(candidate_user, payload, "dossier-key", dossier=True)
    assert (
        await store.request_draft(candidate_user, payload, "dossier-key", dossier=True) == created
    )
    with pytest.raises(DomainError, match="idempotency_key_reused"):
        await store.request_draft(candidate_user, payload, "dossier-key")
    invalid = dossier()
    invalid["evidence"][0]["candidate_quote"] = "Kubernetes production experience"
    gateway = AIGateway([Provider("groq", invalid), Provider("gemini", dossier())], settings)
    assert await DraftWorker(mongo_db, gateway, settings).run_once()
    head = await store.draft(candidate_user, created["draft_id"])
    assert head["provider"] == "gemini" and head["fallback_used"]
    assert head["analysis_letter_version"] == 1
    assert head["source_snapshot"]["profile_id"] == profile["_id"]
    assert (
        head["source_snapshot"]["profile_sha256"]
        == hashlib.sha256(profile["content"].encode()).hexdigest()
    )
    version = await mongo_db.sid_draft_versions.find_one({"draft_id": head["_id"], "version": 1})
    assert version["dossier_analysis"] == head["dossier_analysis"]
    approval = await store.approve_draft(
        candidate_user,
        head["_id"],
        ApproveDraft(expected_version=1, confirmed=True, destination_id="supplied-destination"),
    )
    approved_content = {
        "letter": head["cover_letter"],
        "profile_version": 1,
        "job": payload.job_description,
        "company": payload.company_context,
        "destination": "supplied-destination",
        "dossier_analysis": head["dossier_analysis"],
        "source_snapshot": head["source_snapshot"],
    }
    assert (
        approval["content_hash"]
        == hashlib.sha256(
            json.dumps(approved_content, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
    )
    edited = await store.edit_draft(
        candidate_user,
        head["_id"],
        DraftEdit(expected_version=1, cover_letter="A revised letter reviewed by the candidate."),
    )
    assert edited["analysis_letter_version"] is None and edited["approval_id"] is None
    assert await mongo_db.sid_approvals.count_documents({"active": True}) == 0
    assert await mongo_db.applications.count_documents({}) == 0
    assert (await mongo_db.sid_draft_versions.find_one({"_id": version["_id"]}))[
        "analysis_letter_version"
    ] == 1


@pytest.mark.mongodb
async def test_profile_change_during_generation_prevents_dossier_commit(
    mongo_db, candidate_user, settings
):
    store = Store(mongo_db)
    profile = await accepted_profile(store, candidate_user)
    created = await store.request_draft(
        candidate_user, draft_request(profile), "changed-profile", dossier=True
    )

    async def change():
        await store.edit_profile(
            candidate_user,
            profile["_id"],
            ProfileEdit(expected_version=1, content="Corrected profile with a JavaScript project."),
        )

    gateway = AIGateway([Provider("groq", dossier(), change)], settings)
    await DraftWorker(mongo_db, gateway, settings).run_once()
    head = await store.draft(candidate_user, created["draft_id"])
    assert head["status"] == "failed" and head["version"] == 0
    assert await mongo_db.sid_draft_versions.count_documents({}) == 0


@pytest.mark.mongodb
async def test_dossier_api_uses_candidate_identity_and_rejects_trusted_fields(
    mongo_db, candidate_user, settings
):
    store = Store(mongo_db)
    profile = await accepted_profile(store, candidate_user)
    app = create_app(settings)
    app.dependency_overrides[get_store] = lambda: store
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        data = draft_request(profile).model_dump()
        headers = {"Idempotency-Key": "api-dossier-key"}
        assert (
            await client.post("/api/v1/application-dossiers", json=data, headers=headers)
        ).status_code == 503
        app.dependency_overrides[get_principal] = lambda: candidate_user
        invalid = await client.post(
            "/api/v1/application-dossiers", json={**data, "source_snapshot": {}}, headers=headers
        )
        assert invalid.status_code == 422
        result = await client.post("/api/v1/application-dossiers", json=data, headers=headers)
        assert result.status_code == 202
        assert (await store.draft(candidate_user, result.json()["draft_id"]))["dossier_requested"]


async def ready_dossier(db, user, settings):
    store = Store(db)
    profile = await accepted_profile(store, user)
    created = await store.request_draft(user, draft_request(profile), "reviewable", dossier=True)
    await DraftWorker(db, AIGateway([Provider("groq", dossier())], settings), settings).run_once()
    return store, profile, await store.draft(user, created["draft_id"])


def correction(version=1):
    row = {**dossier()["evidence"][0], "assessment": "not_evidenced", "candidate_quote": None}
    return EvidenceEdit(
        expected_version=version,
        evidence=[row],
        reason="This project does not establish the required experience.",
    )


@pytest.mark.mongodb
async def test_evidence_correction_history_invalidation_and_reapproval(
    mongo_db, candidate_user, settings
):
    store, _, original = await ready_dossier(mongo_db, candidate_user, settings)
    approval_payload = ApproveDraft(expected_version=1, confirmed=True, destination_id="offer-one")
    prior = await store.approve_draft(candidate_user, original["_id"], approval_payload)
    edited = await store.edit_evidence(candidate_user, original["_id"], correction())
    assert edited["version"] == 2 and edited["requires_human_approval"]
    assert edited["approval_id"] is None and edited["status"] == "draft"
    assert edited["analysis_letter_version"] is None
    assert edited["cover_letter"] == original["cover_letter"]
    assert edited["source_snapshot"] == original["source_snapshot"]
    assert edited["analysis_review"]["kind"] == "candidate_corrected"
    assert edited["analysis_review"]["corrected_by"] == candidate_user.user_id
    assert edited["dossier_analysis"]["evidence"][0]["assessment"] == "not_evidenced"
    assert not (await mongo_db.sid_approvals.find_one({"_id": prior["_id"]}))["active"]
    old = await mongo_db.sid_draft_versions.find_one({"draft_id": original["_id"], "version": 1})
    assert old["dossier_analysis"] == original["dossier_analysis"]
    assert old["analysis_review"]["kind"] == "model_proposal"
    new = await store.approve_draft(
        candidate_user, original["_id"], approval_payload.model_copy(update={"expected_version": 2})
    )
    content = {
        "letter": edited["cover_letter"],
        "profile_version": edited["profile_version"],
        "job": edited["job_description"],
        "company": edited["company_context"],
        "destination": "offer-one",
        "dossier_analysis": edited["dossier_analysis"],
        "source_snapshot": edited["source_snapshot"],
        "analysis_review": edited["analysis_review"],
    }
    assert (
        new["content_hash"]
        == hashlib.sha256(
            json.dumps(content, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
    )
    assert new["content_hash"] != prior["content_hash"]
    assert await mongo_db.applications.count_documents({}) == 0
    with pytest.raises(DomainError, match="stale_draft_version"):
        await store.edit_evidence(candidate_user, original["_id"], correction())


@pytest.mark.mongodb
@pytest.mark.parametrize(
    "case", ["invented_quote", "changed_requirement", "stale_profile", "wrong_owner"]
)
async def test_invalid_evidence_correction_does_not_mutate(
    mongo_db, candidate_user, settings, case
):
    store, profile, head = await ready_dossier(mongo_db, candidate_user, settings)
    payload = correction().model_dump()
    user = candidate_user
    code = "invalid_dossier_evidence"
    if case == "invented_quote":
        payload["evidence"][0].update(
            assessment="supported", candidate_quote="Certified Kubernetes expert"
        )
    elif case == "changed_requirement":
        payload["evidence"][0]["requirement"] = "SQL"
        code = "dossier_requirements_immutable"
    elif case == "stale_profile":
        await store.edit_profile(
            candidate_user,
            profile["_id"],
            ProfileEdit(
                expected_version=1, content="A corrected profile with a different project."
            ),
        )
        code = "profile_version_not_validated"
    else:
        user = Principal(user_id="stranger", role="CANDIDATE")
        code = "record_not_found"
    with pytest.raises(DomainError, match=code):
        await store.edit_evidence(user, head["_id"], EvidenceEdit.model_validate(payload))
    assert (await store.draft(candidate_user, head["_id"]))["version"] == 1
    assert await mongo_db.sid_draft_versions.count_documents({"draft_id": head["_id"]}) == 1


@pytest.mark.mongodb
async def test_evidence_correction_race_with_approval_and_rollback(
    mongo_db, candidate_user, settings, monkeypatch
):
    store, _, head = await ready_dossier(mongo_db, candidate_user, settings)
    results = await asyncio.gather(
        store.edit_evidence(candidate_user, head["_id"], correction()),
        store.approve_draft(
            candidate_user,
            head["_id"],
            ApproveDraft(expected_version=1, confirmed=True, destination_id="offer-one"),
        ),
        return_exceptions=True,
    )
    assert isinstance(results[0], dict)
    assert await mongo_db.sid_approvals.count_documents({"active": True}) == 0
    await store.approve_draft(
        candidate_user,
        head["_id"],
        ApproveDraft(expected_version=2, confirmed=True, destination_id="offer-one"),
    )
    original = await store.draft(candidate_user, head["_id"])

    async def fail(*args):
        raise RuntimeError("synthetic evidence history failure")

    monkeypatch.setattr(store, "_draft_version", fail)
    with pytest.raises(RuntimeError, match="synthetic evidence history failure"):
        await store.edit_evidence(candidate_user, head["_id"], correction(2))
    assert await store.draft(candidate_user, head["_id"]) == original
    assert await mongo_db.sid_approvals.count_documents({"active": True}) == 1
    assert await mongo_db.sid_draft_versions.count_documents({"draft_id": head["_id"]}) == 2


@pytest.mark.mongodb
async def test_evidence_correction_api_auth_schema_and_history(mongo_db, candidate_user, settings):
    store, _, head = await ready_dossier(mongo_db, candidate_user, settings)
    app = create_app(settings)
    app.dependency_overrides[get_store] = lambda: store
    path = f"/api/v1/application-drafts/{head['_id']}/evidence"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.put(path, json=correction().model_dump())).status_code == 503
        app.dependency_overrides[get_principal] = lambda: candidate_user
        assert (
            await client.put(path, json={**correction().model_dump(), "analysis_review": {}})
        ).status_code == 422
        response = await client.put(path, json=correction().model_dump())
        assert response.status_code == 200 and response.json()["version"] == 2
        assert (await client.put(path, json=correction().model_dump())).status_code == 409
        old = await client.get(f"/api/v1/application-drafts/{head['_id']}/versions/1")
        assert old.json()["dossier_analysis"]["evidence"][0]["assessment"] == "supported"


@pytest.mark.mongodb
async def test_evidence_edits_reject_plain_letters_and_support_legacy_dossiers(
    mongo_db, candidate_user, settings
):
    store, profile, head = await ready_dossier(mongo_db, candidate_user, settings)
    plain = await store.request_draft(candidate_user, draft_request(profile), "plain-letter")
    await DraftWorker(
        mongo_db, AIGateway([Provider("groq", dossier())], settings), settings
    ).run_once()
    with pytest.raises(DomainError, match="dossier_not_ready"):
        await store.edit_evidence(candidate_user, plain["draft_id"], correction())
    await mongo_db.sid_drafts.update_one({"_id": head["_id"]}, {"$unset": {"analysis_review": ""}})
    edited = await store.edit_evidence(candidate_user, head["_id"], correction())
    assert edited["analysis_review"]["original_version"] == 1
