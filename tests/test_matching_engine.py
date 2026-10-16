"""Matching engine: query composition, retrieval, freshness, rubric ranking, pseudonyms."""

import json

import numpy as np
import pytest
from matching_support import WordEmbedder, demo_settings, qdrant_for

from app.domain import DomainError
from app.identity import Principal
from app.matching.aliases import AliasTable
from app.matching.anonymize import Anonymizer
from app.matching.catalog import PlatformCatalog
from app.matching.engine import MatchEngine, profile_view
from app.matching.index import MatchIndex
from app.matching.models import OfferFilters
from app.matching.results import ProfileView
from app.matching.rubric import Rubric
from app.matching.seed import SEED, seed_platform
from app.matching.worker import MatchSyncWorker

CANDIDATE = Principal(user_id="demo-candidate", role="CANDIDATE")
RECRUITER = Principal(user_id="demo-recruiter", role="RECRUITER", company_id="demo-company")
OTHER_RECRUITER = Principal(user_id="rec-atlas", role="RECRUITER", company_id="co-atlas")
PYTHON_PROFILE = ProfileView(
    skills=["Python", "Django", "Git"],
    statements=["Application web de gestion avec Django et PostgreSQL"],
    text="skill: Python\nskill: Django\nproject: Application web avec Django",
)


@pytest.fixture
async def world(mongo_db, settings, qdrant_http):
    config = demo_settings(settings)
    await seed_platform(mongo_db, config)
    embedder = WordEmbedder()
    qdrant = qdrant_for(qdrant_http, embedder)
    await MatchSyncWorker(mongo_db, qdrant, embedder, config).run_once()
    engine = MatchEngine(
        mongo_db,
        PlatformCatalog(mongo_db, config),
        MatchIndex(mongo_db, qdrant, embedder),
        embedder,
        Rubric(AliasTable.load(), embedder.fingerprint),
        config,
    )
    return engine, mongo_db, embedder


def test_profile_view_reads_accepted_profile_lines():
    view = profile_view(
        "name: Sami\nemail: s@example.test\nskill: Python\nskill: SQL\n"
        "experience: Stage Django\nproject: Site React\neducation: Licence\nheadline: Dev"
    )
    assert view.skills == ["Python", "SQL"]
    assert view.statements == ["Stage Django", "Site React", "Licence", "Dev"]
    assert "Sami" not in view.text and "example.test" not in view.text


async def test_compose_weights_prompt_over_profile_over_keywords(world):
    engine, _, embedder = world
    vector = await engine.compose("python", "accounting", ["docker"])
    cosines = [
        float(np.dot(vector, embedder.vector(word))) for word in ("python", "accounting", "docker")
    ]
    assert cosines[0] > cosines[1] > cosines[2] > 0


async def test_offer_search_applies_filters_and_ranks_by_profile(world):
    engine, _, _ = world
    found = await engine.search_offers(
        CANDIDATE,
        "stage PFE Python web à Sfax",
        profile=PYTHON_PROFILE,
        keywords=["python", "pfe"],
        filters=OfferFilters(location="Sfax", contract="PFE", skills=["Python"]),
    )
    ids = [r.id for r in found.results]
    assert ids and set(ids) <= {
        "off-demo-python-pfe",
        "off-olivia-ml-pfe",
        "off-sfaxnet-iot-pfe",
        "off-thyna-erp-pfe",
    }
    assert ids[0] == "off-demo-python-pfe"
    top = found.results[0]
    assert top.compatibility.percent >= 50 and top.company.city == "sfax"
    assert top.score == pytest.approx(0.6 * top.relevance + 0.4 * top.compatibility.percent / 100)
    assert [r.score for r in found.results] == sorted(
        (r.score for r in found.results), reverse=True
    )


async def test_semantic_skill_filter_accepts_aliases(world):
    engine, _, _ = world
    found = await engine.search_offers(
        CANDIDATE,
        "stage React",
        profile=None,
        keywords=[],
        filters=OfferFilters(location="Sfax", contract="PFE", skills=["React"]),
    )
    ids = {r.id for r in found.results}
    assert "off-demo-react-pfe" in ids and "off-atlas-angular-pfe" not in ids


async def test_unknown_city_returns_no_offers(world):
    engine, _, _ = world
    found = await engine.search_offers(
        CANDIDATE, "Python", profile=None, keywords=[], filters=OfferFilters(location="Atlantis")
    )
    assert found.results == []


async def test_stale_hit_dropped(world):
    engine, db, _ = world
    await db.demo_offers.update_one(
        {"id": "off-demo-python-pfe"}, {"$set": {"description": "Changed after indexing."}}
    )
    found = await engine.search_offers(
        CANDIDATE,
        "stage PFE Python",
        profile=None,
        keywords=[],
        filters=OfferFilters(contract="PFE", skills=["Python"]),
    )
    assert "off-demo-python-pfe" not in {r.id for r in found.results}
    assert found.stale_dropped >= 1


async def test_search_without_profile_has_no_compatibility(world):
    engine, _, _ = world
    found = await engine.search_offers(
        CANDIDATE, "mobile Flutter", profile=None, keywords=[], filters=OfferFilters()
    )
    assert found.results
    assert all(r.compatibility is None and r.score == r.relevance for r in found.results)


async def test_offer_search_is_candidate_only(world):
    engine, _, _ = world
    with pytest.raises(DomainError) as error:
        await engine.search_offers(
            RECRUITER, "Python", profile=None, keywords=[], filters=OfferFilters()
        )
    assert error.value.status == 403


async def test_candidate_search_orders_by_compatibility(world):
    engine, _, _ = world
    offer = await engine.recruiter_offer(RECRUITER, "off-demo-react-pfe")
    found = await engine.search_candidates(
        RECRUITER,
        "frontend ReactJS TypeScript",
        criteria=offer.requirements,
        context=offer.description,
        keywords=[],
        filters=OfferFilters(),
        anonymized=False,
    )
    percents = [r.compatibility.percent for r in found.results]
    assert percents == sorted(percents, reverse=True) and percents[0] >= 40
    top = found.results[0]
    assert top.candidate_id and top.display_name and not top.anonymized
    assert found.criteria == offer.requirements


async def test_anonymized_candidates_use_pseudonyms_without_identity(world):
    engine, _, _ = world
    kwargs = dict(
        criteria=["Node.js", "ReactJS", "MongoDB"],
        context="",
        keywords=[],
        filters=OfferFilters(location="Sfax"),
        anonymized=True,
    )
    first = await engine.search_candidates(RECRUITER, "full stack Node.js ReactJS", **kwargs)
    again = await engine.search_candidates(RECRUITER, "full stack Node.js ReactJS", **kwargs)
    other = await engine.search_candidates(OTHER_RECRUITER, "full stack Node.js ReactJS", **kwargs)
    assert first.results
    blob = json.dumps([r.model_dump() for r in first.results])
    seed = json.loads(SEED.read_text("utf-8"))
    for person in seed["candidates"]:
        assert person["fullName"].split()[-1] not in blob
        assert person["phone"] not in blob
    assert "example.test" not in blob
    for result in first.results:
        assert result.candidate_id is None and result.city is None
        assert result.display_name == result.candidate_ref and result.candidate_ref.startswith("C-")
    assert [r.candidate_ref for r in first.results] == [r.candidate_ref for r in again.results]
    assert not {r.candidate_ref for r in first.results} & {r.candidate_ref for r in other.results}
    resolved = await engine.resolve_candidate_ref(RECRUITER, first.results[0].candidate_ref)
    assert resolved.startswith("cand-")
    with pytest.raises(DomainError) as error:
        await engine.resolve_candidate_ref(OTHER_RECRUITER, first.results[0].candidate_ref)
    assert error.value.status == 404


async def test_resolve_offer_errors(world):
    engine, _, _ = world
    with pytest.raises(DomainError) as closed:
        await engine.resolve_offer(CANDIDATE, "off-demo-closed", "0" * 64)
    assert (closed.value.status, closed.value.code) == (404, "offer_not_accessible")
    with pytest.raises(DomainError) as unverified:
        await engine.resolve_offer(CANDIDATE, "off-pending-react-pfe", "0" * 64)
    assert unverified.value.status == 404
    current = await PlatformCatalog(engine.db, engine.settings).offer("off-demo-react-pfe")
    with pytest.raises(DomainError) as stale:
        await engine.resolve_offer(CANDIDATE, "off-demo-react-pfe", "0" * 64)
    assert (stale.value.status, stale.value.code) == (409, "stale_offer_version")
    resolved = await engine.resolve_offer(CANDIDATE, "off-demo-react-pfe", current.version)
    assert resolved.id == "off-demo-react-pfe"


async def test_index_not_ready_is_503(mongo_db, settings, qdrant_http):
    config = demo_settings(settings)
    embedder = WordEmbedder()
    engine = MatchEngine(
        mongo_db,
        PlatformCatalog(mongo_db, config),
        MatchIndex(mongo_db, qdrant_for(qdrant_http, embedder), embedder),
        embedder,
        Rubric(AliasTable.load(), embedder.fingerprint),
        config,
    )
    with pytest.raises(DomainError) as error:
        await engine.search_offers(
            CANDIDATE, "Python", profile=None, keywords=[], filters=OfferFilters()
        )
    assert (error.value.status, error.value.code) == (503, "matching_index_not_ready")


async def test_score_is_deterministic_and_company_scoped(world):
    engine, _, _ = world
    binding, first = await engine.score(RECRUITER, "off-demo-react-pfe", "cand-02")
    _, second = await engine.score(RECRUITER, "off-demo-react-pfe", "cand-02")
    assert first.model_dump() == second.model_dump()
    assert binding.offer_id == "off-demo-react-pfe"
    assert first.compatibility.percent > 0 and first.explanation_source == "template"
    assert first.explanation.startswith(f"Matching {first.compatibility.percent}")
    with pytest.raises(DomainError) as error:
        await engine.score(OTHER_RECRUITER, "off-demo-react-pfe", "cand-02")
    assert error.value.code == "offer_not_accessible"
    with pytest.raises(DomainError) as missing:
        await engine.score(RECRUITER, "off-demo-react-pfe", "cand-40")
    assert missing.value.code == "candidate_not_accessible"


async def test_market_gaps_count_missing_requirements(world):
    engine, _, _ = world
    gaps = await engine.market_gaps(PYTHON_PROFILE, limit=12)
    assert gaps and all(g["offers"] <= g["of"] <= 12 for g in gaps)
    assert all(g["requirement"] not in {"Python", "Django", "Git"} for g in gaps)
    assert [g["offers"] for g in gaps] == sorted((g["offers"] for g in gaps), reverse=True)


async def test_revalidate_rejects_changed_results(world):
    engine, db, _ = world
    found = await engine.search_offers(
        CANDIDATE, "Flutter mobile", profile=None, keywords=[], filters=OfferFilters()
    )
    response = {"results": [r.model_dump() for r in found.results]}
    await engine.revalidate(CANDIDATE, response)
    await db.demo_offers.update_one(
        {"id": found.results[0].id}, {"$set": {"title": "Renamed offer"}}
    )
    with pytest.raises(DomainError) as error:
        await engine.revalidate(CANDIDATE, response)
    assert (error.value.status, error.value.code) == (409, "search_results_changed")


def test_pseudonyms_are_stable_per_company_and_unlinkable():
    anonymizer = Anonymizer("service-key")
    a = anonymizer.pseudonym("co-1", "cand-01")
    assert a == anonymizer.pseudonym("co-1", "cand-01") and a.startswith("C-") and len(a) == 12
    assert a != anonymizer.pseudonym("co-2", "cand-01")
    assert a != Anonymizer("other-key").pseudonym("co-1", "cand-01")
