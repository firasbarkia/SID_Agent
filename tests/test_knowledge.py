import asyncio
import math
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.knowledge import router
from app.db.connection import server_time
from app.db.queue import LeaseLost
from app.knowledge.embedding import DIMENSIONS, FINGERPRINT, LocalEmbedder, validate_vector
from app.knowledge.models import KnowledgeRecord, Provenance, exact_skill_name
from app.knowledge.normalize import Snapshot, decode_json, normalize, repair_mixed_text
from app.knowledge.qdrant import QdrantIndex, point_id
from app.knowledge.search import KnowledgeSearch
from app.knowledge.store import KnowledgeStore
from app.knowledge.worker import IndexWorker


@pytest.fixture(scope="module")
def normalized():
    return normalize(Path("data/knowledge_base/2026-09-29"))


def reference(label="React", source="onet", source_id="test-react"):
    return KnowledgeRecord(
        source=source,
        kind="skill",
        source_id=source_id,
        labels={"en": label},
        provenance=Provenance(
            source_url="https://example.org/reference",
            source_version="test-1",
            snapshot="test",
            file="test.json",
            sha256="a" * 64,
            license="CC-BY-4.0",
            attribution="Synthetic test reference",
        ),
    )


def test_source_coverage_and_determinism(normalized):
    assert normalized.manifest()["counts"] == {
        "esco:occupation": 4,
        "esco:skill": 11,
        "geonames:location": 4631,
        "onet:occupation": 1016,
        "onet:skill": 8753,
        "rome:occupation": 1911,
        "rome:skill": 35595,
    }
    assert normalized.rejected == []
    assert normalized.manifest() == normalize(Path("data/knowledge_base/2026-09-29")).manifest()
    assert normalized.coverage["onet"]["software_associations"] == 31821
    assert (
        sum(
            len(r.attributes["occupation_associations"])
            for r in normalized.records
            if r.source == "onet" and r.kind == "skill"
        )
        == 31821
    )
    assert len({r.id for r in normalized.records}) == len(normalized.records)


def test_provenance_and_geographic_types(normalized):
    for record in normalized.records:
        assert record.provenance.license and record.provenance.attribution
        assert record.provenance.file in normalized.files
        assert record.provenance.sha256 == normalized.files[record.provenance.file]["sha256"]
    esco = [r for r in normalized.records if r.source == "esco"]
    assert all(not r.provenance.edition_verified for r in esco)
    assert not any("reacting" in r.search_text() for r in esco)
    places = [r for r in normalized.records if r.source == "geonames"]
    assert any(r.attributes["feature_code"] == "ADM1" for r in places)
    assert any(r.attributes["feature_code"] == "PPLH" for r in places)
    assert all(r.attributes["country_code"] == "TN" for r in places)


def test_encoding_and_alias_precision():
    assert decode_json('{"label":"développeur"}'.encode())[1] == "utf-8"
    assert decode_json('{"label":"développeur"}'.encode("cp1252")) == (
        {"label": "développeur"},
        "cp1252",
    )
    assert repair_mixed_text("Macro-compÃ©tence") == "Macro-compétence"
    assert repair_mixed_text("Développement") == "Développement"
    assert repair_mixed_text("لغة برمجة") == "لغة برمجة"
    assert exact_skill_name(" ReactJS ") == exact_skill_name("React.js") == "react"
    assert exact_skill_name("React") != exact_skill_name("Frontend development")
    assert exact_skill_name("reacting to situations") != exact_skill_name("React")
    assert exact_skill_name("Java") != exact_skill_name("JavaScript")
    assert exact_skill_name("Python 3") == exact_skill_name("Python3")
    assert exact_skill_name("Python3") != exact_skill_name("Python2")
    assert exact_skill_name("Python3") != exact_skill_name("Python")


def test_bad_rows_are_quarantined_before_import():
    snapshot = Snapshot()
    values = reference().model_dump()
    values["labels"] = {"en": ""}
    snapshot.add(10, **values)
    assert snapshot.records == []
    assert snapshot.rejected == [{"source": "onet", "row": 10, "reason": "ValidationError"}]
    with pytest.raises((ValueError, UnicodeDecodeError)):
        decode_json(b'{"label": \x81}')


def test_corrupt_source_snapshot_is_rejected(tmp_path):
    source = Path("data/knowledge_base/2026-09-29")
    for name in ("availability_checks.json", "esco_it_seed.json"):
        (tmp_path / name).write_bytes((source / name).read_bytes())
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw/esco_javascript.json").write_bytes(b"{}")
    with pytest.raises(ValueError, match="integrity mismatch"):
        normalize(tmp_path)


@pytest.mark.parametrize("vector", [[1.0], [0.0, 0.0], [math.nan, 1.0], [math.inf, 1.0]])
def test_invalid_vectors_fail_before_transport(vector):
    with pytest.raises(ValueError):
        validate_vector(vector, 2)


def test_versions_have_distinct_point_ids():
    assert point_id("g1", "a", 1, 0) == point_id("g1", "a", 1, 0)
    assert (
        len(
            {
                point_id("g1", "a", 1, 0),
                point_id("g1", "a", 2, 0),
                point_id("g2", "a", 1, 0),
                point_id("g1", "a", 1, 1),
            }
        )
        == 4
    )


class FakeEmbedder:
    dimensions = 3
    fingerprint = "synthetic-test-v1"

    def chunks(self, text):
        return [text]

    def encode(self, texts):
        return [[1.0, 0.5, 0.2] for _ in texts]


@pytest.fixture
async def qdrant_index():
    url = os.environ.get("SID_TEST_QDRANT_URL")
    if not url:
        pytest.skip("Set SID_TEST_QDRANT_URL to opt into Qdrant integration tests")
    async with httpx.AsyncClient(base_url=url, timeout=20) as client:
        yield QdrantIndex(client, FakeEmbedder.dimensions, FakeEmbedder.fingerprint)


@pytest.fixture
async def indexed(mongo_db, qdrant_index, settings):
    store = KnowledgeStore(mongo_db)
    generation = await store.begin_generation(
        FakeEmbedder.fingerprint,
        FakeEmbedder.dimensions,
        prefix="sid_test_",
    )
    await qdrant_index.create(generation)
    try:
        await store.seed_generation(generation)
        worker = IndexWorker(mongo_db, qdrant_index, FakeEmbedder(), settings)
        yield store, generation, worker, qdrant_index
    finally:
        assert generation.startswith("sid_test_") and len(generation) == 41
        await qdrant_index.call("DELETE", qdrant_index.path(generation))


async def drain(worker):
    for _ in range(30):
        if not await worker.run_once():
            return
    raise AssertionError("Unexpected jobs")


@pytest.mark.mongodb
@pytest.mark.qdrant
async def test_import_idempotency_search_and_private_isolation(indexed):
    store, generation, worker, index = indexed
    record = reference()
    snapshot = Snapshot(records=[record])
    assert (await store.import_snapshot(snapshot))["changed"] == 1
    assert (await store.import_snapshot(snapshot))["changed"] == 0
    assert await store.db.sid_index_jobs.count_documents({}) == 1
    await drain(worker)
    await store.promote(generation, index)
    # No Phase 2 outbox or private profile is consumed by this worker.
    await store.db.sid_profiles.insert_one({"_id": "private", "content": "Secret candidate"})
    await index.upsert(
        generation,
        [
            {
                "id": str(uuid4()),
                "vector": [1.0, 0.5, 0.2],
                "payload": {"record_id": "private", "version": 1},
            }
        ],
    )
    search = KnowledgeSearch(store.db, index, FakeEmbedder())
    response = await search.search("ReactJS")
    assert [r["id"] for r in response["results"]] == [record.id]
    assert "Secret candidate" not in str(response)
    assert response["results"][0]["provenance"]["license"] == "CC-BY-4.0"
    assert response["is_compatibility_percentage"] is False
    assert (await search.search("React", source="rome"))["results"] == []
    assert await store.db.sid_profiles.count_documents({}) == 1


@pytest.mark.mongodb
@pytest.mark.qdrant
async def test_updates_revocation_deletion_and_late_writes(indexed):
    store, generation, worker, index = indexed
    record = reference()
    await store.import_batch([record])
    await drain(worker)
    await store.promote(generation, index)
    search = KnowledgeSearch(store.db, index, FakeEmbedder())
    await store.import_batch([reference("ReactJS")])
    # The old vector is immediately hidden, before delivery of the new job.
    assert (await search.search("React"))["results"] == []
    await drain(worker)
    assert (await search.search("React"))["results"][0]["version"] == 2
    await store.set_visibility(record.id, public=False)
    assert (await search.search("React"))["results"] == []
    await drain(worker)
    assert await index.count(generation) == 0
    # Simulate a crash followed by a late old writer; version fencing hides its vector.
    await index.upsert(
        generation,
        [
            {
                "id": point_id(generation, record.id, 1, 0),
                "vector": [1.0, 0.5, 0.2],
                "payload": {"record_id": record.id, "version": 1},
            }
        ],
    )
    assert (await search.search("React"))["results"] == []
    assert await worker.reconcile(generation) == 1
    await store.import_batch([record])
    assert (await store.db.sid_kb_records.find_one({"_id": record.id}))["public"] is False
    await store.set_visibility(record.id, deleted=True)
    await drain(worker)
    assert await index.count(generation) == 0
    await store.import_batch([reference("React new name")])
    assert (await store.db.sid_kb_records.find_one({"_id": record.id}))["deleted"] is True


@pytest.mark.mongodb
@pytest.mark.qdrant
async def test_out_of_order_and_duplicate_jobs_do_not_revert_vectors(indexed):
    store, generation, worker, index = indexed
    await store.import_batch([reference("old")])
    old_task = await worker.queue.claim()
    await store.import_batch([reference("new")])
    new_task = await worker.queue.claim()
    await worker.process(new_task)
    await worker.process(old_task)
    with pytest.raises(LeaseLost):
        await worker.process(new_task)
    assert await index.count(generation) == 1
    await store.promote(generation, index)
    result = await KnowledgeSearch(store.db, index, FakeEmbedder()).search("new")
    assert result["results"][0]["labels"] == {"en": "new"}


@pytest.mark.mongodb
@pytest.mark.qdrant
async def test_actual_write_race_cannot_publish_stale_data(indexed, monkeypatch):
    store, generation, worker, index = indexed
    await store.import_batch([reference("old")])
    task = await worker.queue.claim()
    original = index.upsert

    async def raced(collection, points):
        await store.import_batch([reference("new")])
        await original(collection, points)

    monkeypatch.setattr(index, "upsert", raced)
    await worker.process(task)
    assert await store.db.sid_kb_indexed.count_documents({}) == 0
    monkeypatch.setattr(index, "upsert", original)
    await drain(worker)
    await store.promote(generation, index)
    assert await index.count(generation) == 1


@pytest.mark.mongodb
@pytest.mark.qdrant
async def test_rebuild_restore_and_contract_checks(indexed):
    store, old, worker, index = indexed
    await store.import_batch([reference()])
    await drain(worker)
    await store.promote(old, index)
    new = await store.begin_generation(index.fingerprint, index.dimensions, prefix="sid_test_")
    await index.create(new)
    try:
        await store.seed_generation(new)
        await store.seed_generation(new)  # Resume seeding without duplicate jobs.
        with pytest.raises(ValueError, match="jobs block"):
            await store.promote(new, index)
        assert await store.active(index.fingerprint) == old
        await drain(worker)
        await store.promote(new, index)
        assert await store.active(index.fingerprint) == new
        assert await index.count(new) == await index.count(old) == 1
        wrong = QdrantIndex(index.client, index.dimensions, "different-model")
        with pytest.raises(ValueError, match="contract mismatch"):
            await wrong.verify(new)
        snapshot = await index.snapshot(new)
        assert snapshot["name"].endswith(".snapshot")
        # Native restore of the same collection from a server-local snapshot file.
        await index.delete_points(new, [point_id(new, reference().id, 1, 0)])
        assert await index.count(new) == 0
        await index.call(
            "PUT",
            index.path(new) + "/snapshots/recover?wait=true",
            json={
                "location": f"file:///qdrant/snapshots/{new}/{snapshot['name']}",
                "priority": "snapshot",
            },
        )
        assert await index.count(new) == 1
        await index.verify(new)
    finally:
        await index.call("DELETE", index.path(new))


@pytest.mark.mongodb
@pytest.mark.qdrant
async def test_promotion_detects_concurrent_catalog_change(indexed, monkeypatch):
    store, generation, worker, index = indexed
    await store.import_batch([reference()])
    await drain(worker)
    original = index.count

    async def changed(collection):
        count = await original(collection)
        await store.set_visibility(reference().id, public=False)
        return count

    monkeypatch.setattr(index, "count", changed)
    with pytest.raises(ValueError, match="Catalog changed"):
        await store.promote(generation, index)
    assert (await store.db.sid_kb_catalog.find_one({"_id": "catalog"}))["active"] is None


@pytest.mark.mongodb
@pytest.mark.qdrant
async def test_batched_publish_rolls_back_every_record_on_one_lost_lease(indexed, monkeypatch):
    store, generation, worker, index = indexed
    await store.import_batch([reference(source_id="one"), reference(source_id="two")])
    tasks = [await worker.queue.claim(), await worker.queue.claim()]
    original = index.upsert

    async def lose_lease(collection, points):
        await original(collection, points)
        await store.db.sid_index_jobs.update_one(
            {"_id": tasks[1]["_id"]},
            {"$set": {"lease_token": "another-worker"}},
        )

    monkeypatch.setattr(index, "upsert", lose_lease)
    with pytest.raises(LeaseLost):
        await worker.process_batch(tasks)
    assert await store.db.sid_kb_indexed.count_documents({}) == 0
    assert await store.db.sid_index_jobs.count_documents({"state": "done"}) == 0


@pytest.mark.mongodb
@pytest.mark.qdrant
async def test_batched_jobs_and_parallel_workers(indexed, settings):
    store, generation, worker, index = indexed
    records = [reference(source_id=f"record-{i}") for i in range(32)]
    await asyncio.gather(store.import_batch(records), store.import_batch(records))
    assert await store.db.sid_kb_records.count_documents({}) == 32
    assert await store.db.sid_index_jobs.count_documents({}) == 32
    other = IndexWorker(store.db, index, FakeEmbedder(), settings)
    await asyncio.gather(drain(worker), drain(other))
    assert await store.db.sid_index_jobs.count_documents({"state": "done"}) == 32
    assert await index.count(generation) == 32
    await store.promote(generation, index)


@pytest.mark.mongodb
@pytest.mark.qdrant
async def test_worker_outage_retries_are_bounded(indexed, settings, monkeypatch):
    store, generation, worker, index = indexed
    await store.import_batch([reference()])

    async def unavailable(*args):
        raise httpx.ConnectError("synthetic Qdrant outage")

    monkeypatch.setattr(index, "upsert", unavailable)
    for _ in range(settings.worker_max_attempts):
        await store.db.sid_index_jobs.update_many(
            {},
            {
                "$set": {
                    "available_at": await server_time(store.db),
                }
            },
        )
        assert await worker.run_once()
    assert await store.db.sid_kb_indexed.count_documents({}) == 0
    task = await store.db.sid_index_jobs.find_one({})
    assert task["state"] == "failed" and task["attempts"] == settings.worker_max_attempts
    assert not await worker.run_once()


@pytest.mark.mongodb
@pytest.mark.qdrant
async def test_reclaimed_index_lease_fences_old_worker(indexed):
    from datetime import timedelta

    store, generation, worker, index = indexed
    await store.import_batch([reference()])
    old = await worker.queue.claim()
    await store.db.sid_index_jobs.update_one(
        {"_id": old["_id"]},
        {
            "$set": {
                "lease_until": await server_time(store.db) - timedelta(seconds=1),
            }
        },
    )
    new = await worker.queue.claim()
    assert old["lease_token"] != new["lease_token"]
    with pytest.raises(LeaseLost):
        await worker.process(old)
    assert await store.db.sid_kb_indexed.count_documents({}) == 0
    await worker.process(new)
    await store.promote(generation, index)
    assert await index.count(generation) == 1


@pytest.mark.mongodb
@pytest.mark.qdrant
async def test_full_fastapi_lifecycle_for_public_reference_search(indexed, settings):
    from app.main import create_app

    store, generation, worker, index = indexed
    await store.import_batch([reference()])
    await drain(worker)
    await store.promote(generation, index)
    config = settings.model_copy(
        update={
            "mongodb_uri": settings.mongodb_uri.__class__(os.environ["SID_TEST_MONGODB_URI"]),
            "mongodb_database": store.db.name,
            "qdrant_url": os.environ["SID_TEST_QDRANT_URL"],
        }
    )
    app = create_app(config, knowledge_embedder=FakeEmbedder())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"X-Service-Key": "test-service-key"}
            assert (await client.get("/api/v1/knowledge/status")).status_code == 401
            assert (
                await client.get("/api/v1/knowledge/status", headers=headers)
            ).status_code == 200
            result = await client.post(
                "/api/v1/knowledge/search",
                headers=headers,
                json={"query": "ReactJS", "kind": "skill"},
            )
            assert result.status_code == 200
            assert result.json()["results"][0]["labels"]["en"] == "React"
            # Main user authentication is still deliberately unavailable.
            assert app.state.identity_resolver is None


@pytest.mark.mongodb
async def test_rejected_import_writes_nothing(mongo_db):
    snapshot = Snapshot(records=[reference()], rejected=[{"reason": "bad-row"}])
    with pytest.raises(ValueError, match="Rejected rows"):
        await KnowledgeStore(mongo_db).import_snapshot(snapshot)
    assert await mongo_db.sid_kb_records.count_documents({}) == 0


@pytest.mark.mongodb
async def test_queue_claim_plans_examine_one_job_without_a_blocking_sort(mongo_db):
    now = await server_time(mongo_db)
    await mongo_db.sid_index_jobs.insert_many(
        [
            {
                "_id": str(i),
                "generation": "plan-test",
                "record_id": str(i),
                "version": 1,
                "state": "pending",
                "available_at": now,
                "attempts": 0,
            }
            for i in range(2000)
        ]
    )
    for state, field in (("pending", "available_at"), ("running", "lease_until")):
        result = await mongo_db.command(
            "explain",
            {
                "find": "sid_index_jobs",
                "filter": {"state": state, field: {"$lte": now}},
                "sort": {field: 1, "_id": 1},
                "hint": {"state": 1, field: 1, "_id": 1},
                "limit": 1,
            },
            verbosity="executionStats",
        )
        assert result["executionStats"]["totalDocsExamined"] <= 1
        assert result["executionStats"]["totalKeysExamined"] <= 1


def knowledge_api(settings, service=None):
    app = FastAPI()
    app.state.settings = settings
    app.state.knowledge_search = service
    app.include_router(router)
    return TestClient(app)


def test_reference_api_is_protected_and_disabled_without_configuration(settings):
    with knowledge_api(settings) as client:
        assert client.post("/api/v1/knowledge/search", json={"query": "Python"}).status_code == 401
        response = client.post(
            "/api/v1/knowledge/search",
            json={"query": "Python"},
            headers={"X-Service-Key": "test-service-key"},
        )
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "knowledge_not_configured"


@pytest.mark.parametrize(
    "payload",
    [
        {"query": " "},
        {"query": "a" * 501},
        {"query": "Python", "limit": 11},
        {"query": "Python", "limit": True},
        {"query": "Python", "kind": "candidate"},
        {"query": "Python", "source": "private"},
        {"query": "Python", "tenant_id": "other"},
    ],
)
def test_reference_api_typed_filters_and_bounds(settings, payload):
    with knowledge_api(settings) as client:
        assert (
            client.post(
                "/api/v1/knowledge/search",
                json=payload,
                headers={"X-Service-Key": "test-service-key"},
            ).status_code
            == 422
        )


@pytest.mark.parametrize(
    "error,status,code",
    [
        (RuntimeError("knowledge_busy"), 503, "knowledge_busy"),
        (ValueError("missing index"), 503, "knowledge_index_not_ready"),
        (ValueError("Query exceeds token limit"), 422, "query_token_limit"),
        (httpx.ConnectError("unavailable"), 503, "knowledge_unavailable"),
    ],
)
def test_reference_api_controlled_errors(settings, error, status, code):
    class Service:
        async def search(self, **kwargs):
            raise error

    with knowledge_api(settings, Service()) as client:
        response = client.post(
            "/api/v1/knowledge/search",
            json={"query": "Python"},
            headers={"X-Service-Key": "test-service-key"},
        )
        assert response.status_code == status
        assert response.json()["detail"]["code"] == code


async def test_cancelled_search_retains_admission_until_native_work_finishes(monkeypatch):
    service = KnowledgeSearch(None, None, None, max_concurrent=1)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args):
        entered.set()
        await release.wait()
        return {}

    monkeypatch.setattr(service, "_search", blocked)
    request = asyncio.create_task(service.search("Python"))
    await entered.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    with pytest.raises(RuntimeError, match="knowledge_busy"):
        await service.search("Python")
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert service.running == 0


@pytest.mark.embeddings
def test_real_multilingual_model_smoke():
    directory = os.environ.get("SID_TEST_EMBEDDING_DIRECTORY")
    if not directory:
        pytest.skip("Opt in with SID_TEST_EMBEDDING_DIRECTORY; tests never download weights")
    model = LocalEmbedder(directory)
    assert model.dimensions == DIMENSIONS and model.fingerprint == FINGERPRINT
    chunks = model.chunks("développement web développement web " * 150)
    assert len(chunks) > 1
    assert all(len(model.tokenizer.encode(c).ids) <= 128 for c in chunks)
    texts = [
        "développement logiciel en Python",
        "Python software development",
        "زراعة المحاصيل في الحقول",
        "تطوير البرمجيات بلغة بايثون",
    ]
    vectors = model.encode(texts)
    assert all(len(v) == 384 for v in vectors)

    def similarity(a, b):
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert similarity(vectors[0], vectors[1]) > similarity(vectors[0], vectors[2])
    assert similarity(vectors[1], vectors[3]) > similarity(vectors[1], vectors[2])
    with pytest.raises(ValueError, match="token limit"):
        model.encode(["Python programming " * 500])
