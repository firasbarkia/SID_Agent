"""Versioned match indexes: incremental sync, removal, model switch and the writer lease."""

import json

import pytest
from matching_support import WordEmbedder, demo_settings, qdrant_for

from app.matching.catalog import PlatformCatalog
from app.matching.index import PAYLOAD_FIELDS, MatchIndex
from app.matching.seed import seed_platform
from app.matching.worker import MatchSyncWorker

IDENTITY = ("example.test", "+216", "cv/cand-", "Karray", "Masmoudi", "Ellouze")


@pytest.fixture
async def seeded(mongo_db, settings):
    config = demo_settings(settings)
    await seed_platform(mongo_db, config)
    return mongo_db, config


async def all_points(client, collection):
    response = await client.post(
        f"/collections/{collection}/points/scroll",
        json={"limit": 1000, "with_payload": True, "with_vector": False},
    )
    return response.json()["result"]["points"]


async def test_active_raises_until_first_sync(seeded, qdrant_http):
    db, _ = seeded
    index = MatchIndex(db, qdrant_for(qdrant_http, WordEmbedder()), WordEmbedder())
    with pytest.raises(ValueError, match="matching_index_not_ready"):
        await index.active("offers")


async def test_first_sync_indexes_records_without_identity(seeded, qdrant_http):
    db, config = seeded
    embedder = WordEmbedder()
    index = MatchIndex(db, qdrant_for(qdrant_http, embedder), embedder)
    docs, _ = await PlatformCatalog(db, config).eligible_candidates()
    result = await index.sync("candidates", docs)
    assert result["indexed"] == len(docs) and result["removed"] == 0
    collection = await index.active("candidates")
    points = await all_points(qdrant_http, collection)
    assert {p["payload"]["record_id"] for p in points} == {d.id for d in docs}
    assert all(set(p["payload"]) <= set(PAYLOAD_FIELDS) for p in points)
    stored = await db.sid_match_records.find({"kind": "candidates"}).to_list()
    blob = json.dumps([p["payload"] for p in points]) + json.dumps(
        [r["doc"] for r in stored], default=str
    )
    assert not any(secret in blob for secret in IDENTITY)
    sample = stored[0]
    assert sample["phrases"]["skills"] and sample["version"] == next(
        d.version for d in docs if d.id == sample["record_id"]
    )


async def test_unchanged_sync_embeds_nothing(seeded, qdrant_http):
    db, config = seeded
    embedder = WordEmbedder()
    index = MatchIndex(db, qdrant_for(qdrant_http, embedder), embedder)
    docs, _ = await PlatformCatalog(db, config).eligible_offers()
    await index.sync("offers", docs)
    calls = embedder.encoded
    result = await index.sync("offers", docs)
    assert result == {"indexed": 0, "removed": 0, "total": len(docs)}
    assert embedder.encoded == calls


async def test_changed_record_is_reembedded_and_old_points_removed(seeded, qdrant_http):
    db, config = seeded
    embedder = WordEmbedder()
    index = MatchIndex(db, qdrant_for(qdrant_http, embedder), embedder)
    catalog = PlatformCatalog(db, config)
    await index.sync("candidates", (await catalog.eligible_candidates())[0])
    await db.demo_candidates.update_one({"id": "cand-01"}, {"$push": {"parsedSkills": "Docker"}})
    docs, _ = await catalog.eligible_candidates()
    result = await index.sync("candidates", docs)
    assert result["indexed"] == 1
    new_version = next(d.version for d in docs if d.id == "cand-01")
    points = await all_points(qdrant_http, await index.active("candidates"))
    assert {p["payload"]["version"] for p in points if p["payload"]["record_id"] == "cand-01"} == {
        new_version
    }


async def test_closed_offer_is_removed(seeded, qdrant_http):
    db, config = seeded
    embedder = WordEmbedder()
    index = MatchIndex(db, qdrant_for(qdrant_http, embedder), embedder)
    catalog = PlatformCatalog(db, config)
    await index.sync("offers", (await catalog.eligible_offers())[0])
    await db.demo_offers.update_one({"id": "off-demo-react-pfe"}, {"$set": {"status": "CLOSED"}})
    result = await index.sync("offers", (await catalog.eligible_offers())[0])
    assert result["removed"] == 1
    points = await all_points(qdrant_http, await index.active("offers"))
    assert "off-demo-react-pfe" not in {p["payload"]["record_id"] for p in points}
    assert (
        await db.sid_match_records.find_one({"kind": "offers", "record_id": "off-demo-react-pfe"})
        is None
    )


async def test_fingerprint_change_builds_new_collection_and_switches(seeded, qdrant_http):
    db, config = seeded
    old_embedder = WordEmbedder("match-test-v1")
    old = MatchIndex(db, qdrant_for(qdrant_http, old_embedder), old_embedder)
    docs, _ = await PlatformCatalog(db, config).eligible_offers()
    await old.sync("offers", docs)
    first = await old.active("offers")
    new_embedder = WordEmbedder("match-test-v2")
    new = MatchIndex(db, qdrant_for(qdrant_http, new_embedder), new_embedder)
    with pytest.raises(ValueError):
        await new.active("offers")
    result = await new.sync("offers", docs)
    assert result["indexed"] == len(docs)
    second = await new.active("offers")
    assert second != first
    collections = (await qdrant_http.get("/collections")).json()["result"]["collections"]
    assert first not in {c["name"] for c in collections}
    assert await db.sid_match_records.count_documents({"collection": first}) == 0


async def test_query_returns_closest_records(seeded, qdrant_http):
    db, config = seeded
    embedder = WordEmbedder()
    index = MatchIndex(db, qdrant_for(qdrant_http, embedder), embedder)
    await index.sync("offers", (await PlatformCatalog(db, config).eligible_offers())[0])
    hits = await index.query(
        "offers", embedder.vector("Flutter Dart Firebase mobile"), limit=5, filters={}
    )
    assert hits[0]["record_id"] == "off-medina-flutter-pfe"
    assert len({h["record_id"] for h in hits}) == len(hits)
    pfe = await index.query(
        "offers", embedder.vector("Python"), limit=30, filters={"contract": "PFE", "city": "tunis"}
    )
    assert [h["record_id"] for h in pfe] == ["off-carthage-python-pfe", "off-carthage-devops-pfe"]


async def test_worker_lease_prevents_concurrent_writers(seeded, qdrant_http):
    db, config = seeded
    embedder = WordEmbedder()
    qdrant = qdrant_for(qdrant_http, embedder)
    first = MatchSyncWorker(db, qdrant, embedder, config, owner="worker-a")
    second = MatchSyncWorker(db, qdrant, embedder, config, owner="worker-b")
    assert (await first.run_once())["status"] == "synced"
    assert (await second.run_once())["status"] == "lease_held"
    assert (await first.run_once())["status"] == "synced"
    status = await MatchIndex(db, qdrant, embedder).status()
    assert status["offers"]["records"] > 0 and status["candidates"]["records"] > 0


async def test_lost_qdrant_collection_is_rebuilt(seeded, qdrant_http):
    db, config = seeded
    embedder = WordEmbedder()
    index = MatchIndex(db, qdrant_for(qdrant_http, embedder), embedder)
    docs, _ = await PlatformCatalog(db, config).eligible_offers()
    await index.sync("offers", docs)
    await qdrant_http.delete(f"/collections/{await index.active('offers')}")
    result = await index.sync("offers", docs)
    assert result["indexed"] == len(docs)
    hits = await index.query("offers", embedder.vector("Flutter Dart"), limit=3, filters={})
    assert hits


class FailingEmbedder(WordEmbedder):
    def encode(self, texts):
        raise ValueError("Local embedding inference failed")


async def test_failed_build_reuses_its_collection(seeded, qdrant_http):
    db, config = seeded
    docs, _ = await PlatformCatalog(db, config).eligible_offers()
    before = {
        c["name"] for c in (await qdrant_http.get("/collections")).json()["result"]["collections"]
    }
    failing = FailingEmbedder()
    for _ in range(2):
        with pytest.raises(ValueError):
            await MatchIndex(db, qdrant_for(qdrant_http, failing), failing).sync("offers", docs)
    working = WordEmbedder()
    await MatchIndex(db, qdrant_for(qdrant_http, working), working).sync("offers", docs)
    after = {
        c["name"] for c in (await qdrant_http.get("/collections")).json()["result"]["collections"]
    }
    assert len({name for name in after - before if name.startswith("sid_match_offers_")}) == 1


async def test_released_lease_lets_a_new_worker_sync(seeded, qdrant_http):
    db, config = seeded
    embedder = WordEmbedder()
    qdrant = qdrant_for(qdrant_http, embedder)
    first = MatchSyncWorker(db, qdrant, embedder, config, owner="worker-a")
    assert (await first.run_once())["status"] == "synced"
    await first.release()
    second = MatchSyncWorker(db, qdrant, embedder, config, owner="worker-b")
    assert (await second.run_once())["status"] == "synced"
