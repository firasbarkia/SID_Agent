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
from app.dossier import validate_dossier
from app.identity import get_principal
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
