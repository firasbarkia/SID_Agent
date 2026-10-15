import pytest
from pydantic import ValidationError

from app.domain import DomainError
from app.identity import Principal
from app.matching.models import OfferFilters, SyntheticCorpus
from app.matching.synthetic import SyntheticOfferSearch, fixture_filters


@pytest.fixture
def search():
    return SyntheticOfferSearch.from_fixture()


async def find(search, user, query="Python SQL", filters=None, profile=None, keywords=()):
    return await search.search(
        user, query, profile=profile, keywords=keywords, filters=filters or OfferFilters()
    )


async def test_hard_filters_exclude_private_closed_wrong_location_and_contract(
    search, candidate_user
):
    result = await find(
        search,
        candidate_user,
        filters=OfferFilters(location="Sfax", contract="PFE", skills=["Python", "SQL"]),
    )
    assert [offer.id for offer in result.results] == ["fixture-python-pfe"]
    assert result.results[0].source == "synthetic"
    assert result.results[0].is_compatibility_percentage is False
    assert result.results[0].binding.snapshot_sha256


async def test_multilingual_aliases_and_junior_profile_enrich_ranking(search, candidate_user):
    react = await find(
        search,
        candidate_user,
        "développement React",
        OfferFilters(location="Sfax", contract="PFE", skills=["ReactJS"]),
    )
    assert [offer.id for offer in react.results] == ["fixture-react-pfe"]
    filters = OfferFilters(location="Sfax", contract="PFE")
    without = await find(search, candidate_user, "web development PFE", filters)
    with_profile = await find(
        search,
        candidate_user,
        "web development PFE",
        filters,
        {"content": "University project using Python and SQL"},
    )
    scores = {offer.id: offer.ranking_score for offer in without.results}
    assert with_profile.results[0].id == "fixture-python-pfe"
    assert with_profile.results[0].ranking_score != scores["fixture-python-pfe"]


async def test_no_results_is_not_filled_with_unrelated_records(search, candidate_user):
    for filters in (
        OfferFilters(location="Sousse"),
        OfferFilters(skills=["Rust"]),
        OfferFilters(contract="EMPLOYMENT", skills=["Python"]),
    ):
        assert not (await find(search, candidate_user, filters=filters)).results
    assert not (await find(search, candidate_user, "unrelated finance")).results


async def test_replace_fences_changes_closure_and_deletion(search, candidate_user):
    original = SyntheticOfferSearch.from_fixture()
    record = original.resolve(candidate_user, "fixture-python-pfe", 1)
    generation = search.generation
    changed = record.model_copy(update={"version": 2, "status": "CLOSED"})
    search.replace(SyntheticCorpus(synthetic=True, version="changed", offers=[changed]))
    assert search.generation != generation
    assert not (await find(search, candidate_user)).results
    with pytest.raises(DomainError, match="offer_not_accessible"):
        search.resolve(candidate_user, record.id, 1)
    search.replace(SyntheticCorpus(synthetic=True, version="deleted", offers=[]))
    assert not (await find(search, candidate_user)).results


async def test_index_is_a_detached_snapshot_and_never_accepts_real_sources(search, candidate_user):
    record = search.resolve(candidate_user, "fixture-python-pfe", 1)
    record.title = "mutated externally"
    assert search.resolve(candidate_user, record.id, 1).title == "Python web PFE"
    with pytest.raises(DomainError, match="stale_offer_version"):
        search.resolve(candidate_user, record.id, 99)
    with pytest.raises(ValidationError):
        type(record).model_validate({**record.model_dump(), "source": "platform"})


async def test_recruiter_cannot_use_candidate_offer_adapter(search):
    recruiter = Principal(user_id="fixture-recruiter", role="RECRUITER", company_id="company")
    with pytest.raises(DomainError, match="candidate_access_required"):
        await find(search, recruiter)


async def test_top_five_is_bounded_deterministic_and_generation_bound(search, candidate_user):
    base = search.resolve(candidate_user, "fixture-python-pfe", 1)
    records = [base.model_copy(update={"id": f"fixture-{i}"}) for i in range(8)]
    search.replace(SyntheticCorpus(synthetic=True, version="eight", offers=records))
    result = await find(search, candidate_user)
    assert [offer.id for offer in result.results] == [f"fixture-{i}" for i in range(5)]
    assert result.generation == search.generation
    assert (await find(search, candidate_user)).model_dump() == result.model_dump()


def test_simulated_followup_changes_location_without_dropping_other_filters():
    filters = fixture_filters("Je cherche un stage PFE Python à Sfax")
    changed = fixture_filters("Tunis instead", ["Sfax", "Python"], filters.model_dump())
    assert changed.location == "Tunis" and changed.contract == "PFE"
    assert changed.skills == ["Python"]
    assert fixture_filters("And SQL", [], changed.model_dump()).location == "Tunis"
