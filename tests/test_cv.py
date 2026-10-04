import asyncio
import io
import json
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)

from app.ai.errors import ProvidersUnavailable
from app.ai.providers import Generation
from app.api.persistence import get_store
from app.cv.models import (
    Corrections,
    Extraction,
    evaluate,
    grounded,
    review_warnings,
)
from app.cv.pdf import MAX_BYTES, MAX_MEMORY, MAX_STREAM, extract_pdf, parse_pdf
from app.cv.store import CVStore
from app.cv.worker import CVWorker
from app.db.connection import server_time
from app.db.queue import LeaseLost, MongoQueue
from app.db.store import Store
from app.domain import DomainError, ProfileCreate, ProfileEdit
from app.identity import Principal, get_principal
from app.main import create_app

CORPUS = json.loads((Path(__file__).parent / "fixtures/cv-corpus.json").read_text("utf-8"))


def pdf(lines, *, pages=1, password=None):
    """Minimal in-memory PDF fixture, no document artifacts or paid services."""
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        escaped = [
            line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for line in lines
        ]
        commands = (
            "BT /F1 12 Tf 20 TL 50 740 Td "
            + " ".join(f"({line}) Tj T*" for line in escaped)
            + " ET"
        )
        stream.set_data(commands.encode("cp1252"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    if password:
        writer.encrypt(password)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def extraction(case):
    return {
        "facts": [
            {"field": field, "value": value, "evidence": {"page": 1, "quote": value}}
            for field, value in zip(case["fields"], case["lines"], strict=False)
        ]
    }


@pytest.mark.parametrize("case", CORPUS["cases"], ids=lambda c: c["id"])
async def test_synthetic_pdf_evidence_and_completeness(case):
    pages = await extract_pdf(pdf(case["lines"]))
    for line in case["lines"]:
        assert line in pages[0]["text"]
    facts, warnings = grounded(Extraction.model_validate(extraction(case)), pages)
    assert not warnings
    assert evaluate(facts)["score"] == case["score"]
    if case["language"] == "en":
        assert review_warnings(facts) == ["conflicting_email", "ambiguous_date_review_required"]
        assert not any("PhD" in fact["value"] for fact in facts)


@pytest.mark.parametrize(
    "data,code",
    [
        (b"not a pdf", "invalid_pdf"),
        (b"%PDF-1.7\ntruncated", "invalid_pdf"),
        (pdf(["A long enough synthetic CV"], password="secret"), "encrypted_pdf"),
        (pdf([]), "cv_ocr_or_text_required"),
        (pdf(["A long enough synthetic CV"], pages=11), "cv_page_limit"),
        (pdf(["a" * 16001]), "cv_text_limit"),
        (b"%PDF-" + b"x" * MAX_BYTES, "cv_file_too_large"),
    ],
    ids=[
        "not-pdf",
        "truncated",
        "encrypted",
        "image-only",
        "page-limit",
        "text-limit",
        "byte-limit",
    ],
)
def test_bad_pdfs_fail_explicitly(data, code):
    with pytest.raises(DomainError, match=code):
        parse_pdf(data)


async def test_parser_subprocess_failure_and_timeout():
    with pytest.raises(DomainError, match="invalid_pdf"):
        await extract_pdf(b"invalid")
    with pytest.raises(DomainError, match="cv_parser_timeout"):
        await extract_pdf(pdf(["A long enough synthetic CV"]), timeout=0.001)


def test_oversized_decoded_content_stream_is_rejected():
    with pytest.raises(DomainError, match="cv_stream_limit"):
        parse_pdf(pdf(["x" * (MAX_STREAM + 1)]))


async def test_memory_monitor_stops_disposable_parser(monkeypatch):
    import app.cv.pdf as parser

    monkeypatch.setattr(
        parser.psutil.Process, "memory_info", lambda _: SimpleNamespace(rss=MAX_MEMORY + 1)
    )
    with pytest.raises(DomainError, match="cv_parser_resource_limit"):
        await extract_pdf(pdf(["A long enough synthetic CV"]))


async def test_cancellation_kills_parser_process(monkeypatch):
    import app.cv.pdf as parser

    create = asyncio.create_subprocess_exec
    started = asyncio.Event()
    processes = []

    async def slow_parser(*args, **kwargs):
        process = await create(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(parser.asyncio, "create_subprocess_exec", slow_parser)
    task = asyncio.create_task(extract_pdf(b"synthetic"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].returncode is not None


def test_hallucinated_values_wrong_pages_and_wrong_quotes_are_removed():
    result = Extraction.model_validate(
        {
            "facts": [
                {"field": "skill", "value": "Python", "evidence": {"page": 1, "quote": "Python"}},
                {"field": "education", "value": "PhD", "evidence": {"page": 1, "quote": "Python"}},
                {"field": "skill", "value": "Java", "evidence": {"page": 1, "quote": "Java"}},
                {"field": "skill", "value": "Python", "evidence": {"page": 2, "quote": "Python"}},
                {"field": "skill", "value": "Java", "evidence": {"page": 1, "quote": "JavaScript"}},
            ]
        }
    )
    facts, warnings = grounded(result, [{"page": 1, "text": "Python student profile. JavaScript"}])
    assert [f["value"] for f in facts] == ["Python"]
    assert warnings == ["unsupported_fact_removed"]


def test_correction_cannot_forge_evidence_and_rubric_credits_projects():
    with pytest.raises(ValidationError):
        Corrections.model_validate(
            {
                "expected_version": 1,
                "facts": [
                    {
                        "field": "skill",
                        "value": "Python",
                        "origin": "pdf",
                    }
                ],
            }
        )
    with pytest.raises(ValidationError):
        Corrections.model_validate(
            {
                "expected_version": 1,
                "facts": [
                    {"field": "skill", "value": "Python"},
                    {"field": "skill", "value": "python"},
                ],
            }
        )
    assert evaluate([])["score"] == 0
    assert evaluate([{"field": "project", "value": "University project"}])["score"] == 20
    assert evaluate([{"field": "experience", "value": "Company internship"}])["score"] == 20


class Gateway:
    def __init__(self, result=None, action=None):
        self.result = result or extraction(CORPUS["cases"][0])
        self.action = action
        self.calls = 0
        self.prompt = None

    async def generate(self, system, prompt, *, validate=None):
        self.calls += 1
        self.prompt = json.loads(prompt)
        if self.action:
            await self.action()
        if isinstance(self.result, Exception):
            raise self.result
        text = self.result if isinstance(self.result, str) else json.dumps(self.result)
        return Generation(text, "groq", "synthetic-test-model")


async def queued(db, user, *, key="cv-request-one", case=None):
    case = case or CORPUS["cases"][0]
    await db.users.update_one(
        {"id": user.user_id},
        {
            "$set": {
                "role": "CANDIDATE",
                "isActive": True,
            }
        },
        upsert=True,
    )
    return await CVStore(db).upload(user, pdf(case["lines"]), key, case["language"])


@pytest.mark.mongodb
async def test_import_review_correct_accept_history_and_no_silent_overwrite(
    mongo_db,
    candidate_user,
    settings,
):
    store = CVStore(mongo_db)
    existing = await Store(mongo_db).create_profile(
        candidate_user, ProfileCreate(content="An existing profile that must stay unchanged.")
    )
    head = await queued(mongo_db, candidate_user)
    assert await CVWorker(mongo_db, Gateway(), settings).run_once()
    review = await store.get(candidate_user, head["_id"])
    assert review["status"] == "review" and review["version"] == 1
    assert await mongo_db.sid_profiles.count_documents({}) == 1
    corrections = Corrections(
        expected_version=1,
        facts=[{"field": f["field"], "value": f["value"]} for f in review["facts"]]
        + [{"field": "phone", "value": "+216 00000000"}],
    )
    updated = await store.edit(candidate_user, head["_id"], corrections)
    assert updated["facts"][-1]["origin"] == "candidate_asserted"
    assert updated["facts"][0]["origin"] == "pdf"
    with pytest.raises(DomainError, match="cv_version_not_editable"):
        await store.edit(candidate_user, head["_id"], corrections)
    with pytest.raises(DomainError, match="stale_cv_version"):
        await store.accept(candidate_user, head["_id"], 1)
    accepted = await asyncio.gather(
        *(store.accept(candidate_user, head["_id"], 2) for _ in range(3))
    )
    assert len({h["profile_id"] for h in accepted}) == 1
    assert await mongo_db.sid_profiles.count_documents({}) == 2
    profile = await Store(mongo_db).profile(candidate_user, accepted[0]["profile_id"])
    assert profile["validated_version"] == 1
    assert profile["structured_data"]["cv_version"] == 2
    assert (await mongo_db.sid_profiles.find_one({"_id": existing["_id"]}))["version"] == 1
    assert await mongo_db.sid_cv_versions.count_documents({"import_id": head["_id"]}) == 2
    assert (
        await mongo_db.sid_outbox.count_documents(
            {
                "aggregate_id": profile["_id"],
                "type": "profile.accepted",
            }
        )
        == 1
    )
    await Store(mongo_db).edit_profile(
        candidate_user,
        profile["_id"],
        ProfileEdit(
            expected_version=1, content="Candidate supplied a different plain-text profile."
        ),
    )
    edited = await Store(mongo_db).profile(candidate_user, profile["_id"])
    assert edited["structured_data"] is None and edited["validated_version"] is None
    snapshot = await mongo_db.sid_profile_versions.find_one(
        {
            "profile_id": profile["_id"],
            "version": 1,
        }
    )
    assert snapshot["structured_data"]["facts"] == profile["structured_data"]["facts"]


@pytest.mark.mongodb
async def test_idempotent_upload_ownership_conflicts_and_private_deletion(
    mongo_db,
    candidate_user,
    settings,
):
    store = CVStore(mongo_db)
    heads = await asyncio.gather(
        *(queued(mongo_db, candidate_user, case=CORPUS["cases"][1]) for _ in range(3))
    )
    assert len({h["_id"] for h in heads}) == 1
    assert await mongo_db.sid_cv_files.count_documents({}) == 1
    assert await mongo_db.sid_cv_tasks.count_documents({}) == 1
    head = heads[0]
    stranger = Principal(user_id="stranger", role="CANDIDATE")
    for operation in (
        store.get(stranger, head["_id"]),
        store.delete(stranger, head["_id"]),
        store.accept(stranger, head["_id"], 1),
    ):
        with pytest.raises(DomainError, match="record_not_found"):
            await operation
    with pytest.raises(DomainError, match="idempotency_key_reused"):
        await store.upload(candidate_user, b"changed", "cv-request-one", "en")
    assert await CVWorker(mongo_db, Gateway(extraction(CORPUS["cases"][1])), settings).run_once()
    with pytest.raises(DomainError, match="cv_conflicts_require_correction"):
        await store.accept(candidate_user, head["_id"], 1)
    assert await mongo_db.sid_profiles.count_documents({}) == 0
    assert await mongo_db.sid_kb_records.count_documents({}) == 0
    await store.delete(candidate_user, head["_id"])
    for collection in ("sid_cv_files", "sid_cv_versions", "sid_cv_tasks"):
        assert await mongo_db[collection].count_documents({}) == 0
    with pytest.raises(DomainError, match="cv_import_deleted"):
        await queued(mongo_db, candidate_user, case=CORPUS["cases"][1])


@pytest.mark.mongodb
@pytest.mark.parametrize("kind", ["malformed", "unavailable", "unsupported", "inactive"])
async def test_worker_controlled_failures(mongo_db, candidate_user, settings, kind):
    head = await queued(mongo_db, candidate_user)
    if kind == "inactive":
        await mongo_db.users.update_one(
            {"id": candidate_user.user_id}, {"$set": {"isActive": False}}
        )
    result = {
        "malformed": "not json",
        "unavailable": ProvidersUnavailable(5),
        "unsupported": {
            "facts": [
                {"field": "skill", "value": "PhD", "evidence": {"page": 1, "quote": "Python"}}
            ]
        },
    }
    gateway = Gateway(result.get(kind))
    assert await CVWorker(mongo_db, gateway, settings).run_once()
    current = await CVStore(mongo_db).get(candidate_user, head["_id"])
    task = await mongo_db.sid_cv_tasks.find_one({"import_id": head["_id"]})
    if kind == "unavailable":
        assert current["status"] == "queued" and task["state"] == "pending"
    elif kind == "unsupported":
        assert current["facts"] == [] and current["warnings"] == ["unsupported_fact_removed"]
        with pytest.raises(DomainError, match="profile_content_size_invalid"):
            await CVStore(mongo_db).accept(candidate_user, head["_id"], 1)
    else:
        assert current["status"] == "failed" and task["state"] == "failed"
        if kind == "inactive":
            assert gateway.calls == 0
    assert await mongo_db.sid_profiles.count_documents({}) == 0


@pytest.mark.mongodb
async def test_delete_during_generation_fences_late_result(mongo_db, candidate_user, settings):
    head = await queued(mongo_db, candidate_user)

    async def delete():
        await CVStore(mongo_db).delete(candidate_user, head["_id"])

    assert await CVWorker(mongo_db, Gateway(action=delete), settings).run_once()
    assert await mongo_db.sid_cv_versions.count_documents({}) == 0
    assert await mongo_db.sid_profiles.count_documents({}) == 0
    assert (await mongo_db.sid_cv_imports.find_one({"_id": head["_id"]}))["status"] == "deleted"


@pytest.mark.mongodb
async def test_expired_cv_lease_cannot_finish(mongo_db, candidate_user, settings):
    head = await queued(mongo_db, candidate_user)
    queue = MongoQueue(mongo_db, "sid_cv_tasks")
    old = await queue.claim()
    await mongo_db.sid_cv_tasks.update_one(
        {"_id": old["_id"]},
        {
            "$set": {
                "lease_until": await server_time(mongo_db) - timedelta(seconds=1),
            }
        },
    )
    replacement = await queue.claim()
    assert replacement["lease_token"] != old["lease_token"]
    with pytest.raises(LeaseLost):
        await queue.finish(old)
    assert (await CVStore(mongo_db).get(candidate_user, head["_id"]))["status"] == "queued"


@pytest.mark.mongodb
async def test_worker_invalid_pdf_never_calls_ai(mongo_db, candidate_user, settings):
    await mongo_db.users.insert_one(
        {"id": candidate_user.user_id, "role": "CANDIDATE", "isActive": True}
    )
    head = await CVStore(mongo_db).upload(candidate_user, b"%PDF-1.7 invalid", "bad-pdf-key", "fr")
    gateway = Gateway()
    assert await CVWorker(mongo_db, gateway, settings).run_once()
    assert gateway.calls == 0
    failed = await CVStore(mongo_db).get(candidate_user, head["_id"])
    assert failed["status"] == "failed" and failed["error_code"] == "invalid_pdf"


@pytest.mark.mongodb
async def test_atomic_extraction_rolls_back_when_history_write_fails(
    mongo_db,
    candidate_user,
    settings,
    monkeypatch,
):
    head = await queued(mongo_db, candidate_user)
    worker = CVWorker(mongo_db, Gateway(), settings)

    async def fail(*args):
        raise RuntimeError("Synthetic history failure")

    monkeypatch.setattr(worker.store, "version", fail)
    with pytest.raises(RuntimeError, match="Synthetic history failure"):
        await worker.run_once()
    current = await CVStore(mongo_db).get(candidate_user, head["_id"])
    assert current["status"] == "queued" and current["version"] == 0
    assert await mongo_db.sid_cv_versions.count_documents({}) == 0
    task = await mongo_db.sid_cv_tasks.find_one({"import_id": head["_id"]})
    assert task["state"] == "running"  # Lease recovery can retry after worker failure.


@pytest.mark.mongodb
async def test_account_deactivation_during_extraction_blocks_result(
    mongo_db,
    candidate_user,
    settings,
):
    head = await queued(mongo_db, candidate_user)

    async def deactivate():
        await mongo_db.users.update_one(
            {"id": candidate_user.user_id}, {"$set": {"isActive": False}}
        )

    assert await CVWorker(mongo_db, Gateway(action=deactivate), settings).run_once()
    failed = await CVStore(mongo_db).get(candidate_user, head["_id"])
    assert failed["status"] == "failed" and failed["error_code"] == "inactive_or_unknown_user"
    assert await mongo_db.sid_profiles.count_documents({}) == 0


async def test_cv_api_fails_closed_without_platform_identity(settings):
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        for path in ("/api/v1/cv-imports/unknown", "/api/v1/profiles/unknown/evaluation"):
            response = await c.get(path, headers={"X-Service-Key": "test-service-key"})
            assert response.status_code == 503
            assert response.json()["detail"]["code"] == "platform_identity_not_integrated"


@pytest.mark.mongodb
async def test_cv_api_upload_review_confirm_validation_and_evaluation(
    mongo_db,
    candidate_user,
    settings,
):
    app = create_app(settings)

    async def identity():
        return candidate_user

    app.dependency_overrides[get_principal] = identity
    app.dependency_overrides[get_store] = lambda: Store(mongo_db)
    data = pdf(CORPUS["cases"][0]["lines"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        path = "/api/v1/cv-imports"
        headers = {"Content-Type": "application/pdf", "Idempotency-Key": "request-cv-api"}
        assert (
            await c.post(path, content=data, headers={"Idempotency-Key": "request-cv-api"})
        ).status_code == 415
        assert (await c.post(path, content=b"bad", headers=headers)).status_code == 422
        assert (
            await c.post(path, content=b"%PDF-" + b"x" * MAX_BYTES, headers=headers)
        ).status_code == 413
        response = await c.post(path, content=data, headers=headers)
        assert response.status_code == 202
        head = response.json()
        assert head["requires_human_approval"] and "data" not in head
        assert "idempotency_key" not in head
        await mongo_db.users.insert_one(
            {"id": candidate_user.user_id, "role": "CANDIDATE", "isActive": True}
        )
        await CVWorker(mongo_db, Gateway(), settings).run_once()
        path += "/" + head["id"]
        assert (
            await c.post(path + "/accept", json={"expected_version": 1, "confirmed": False})
        ).status_code == 422
        assert (
            await c.post(path + "/accept", json={"expected_version": 1, "confirmed": "true"})
        ).status_code == 422
        accepted = await c.post(path + "/accept", json={"expected_version": 1, "confirmed": True})
        assert accepted.status_code == 200
        result = await c.get("/api/v1/profiles/" + accepted.json()["profile_id"] + "/evaluation")
        assert result.status_code == 200 and result.json()["score"] == 100
        assert (await c.get(path + "/versions/1")).status_code == 200
        assert (await c.delete(path)).status_code == 204
        assert (await c.get(path)).status_code == 410
        candidate_user = Principal(user_id="recruiter", role="RECRUITER", company_id="company")
        assert (await c.get("/api/v1/cv-imports/anything")).status_code == 403
