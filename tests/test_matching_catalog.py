"""Read-only projection of platform UML records into searchable documents."""

import json

import pytest

from app.matching.aliases import AliasTable, normalize_term
from app.matching.catalog import PlatformCatalog, city_of, project_candidate, project_offer
from app.matching.seed import SEED, seed_platform

COMPANY = {
    "id": "co-1",
    "userId": "rec-1",
    "companyName": "Atlas Soft",
    "sector": "Software",
    "address": "Route de Tunis km 4, Sfax",
    "description": "Web product startup.",
    "isVerified": True,
}
OFFER = {
    "id": "of-1",
    "companyId": "co-1",
    "title": "Stage PFE React",
    "type": "STAGE_PFE",
    "description": "Développer des interfaces web avec ReactJS et Git.",
    "requiredSkills": ["ReactJS", "Git"],
    "status": "OPEN",
}
CANDIDATE = {
    "id": "cp-1",
    "userId": "u-1",
    "fullName": "Amira Synthetique",
    "phone": "+216 00 000 001",
    "city": "Sfax",
    "parsedSkills": ["Python", {"name": "SQL"}],
    "parsedExperiences": [
        {"title": "Stage", "company": "Orbit", "description": "Built a Django API"}
    ],
    "parsedEducation": ["Licence informatique"],
    "isNewCandidate": True,
    "rawCvUrl": "https://example.test/cv.pdf",
}
USER = {"id": "u-1", "role": "CANDIDATE", "isActive": True, "email": "amira@example.test"}


def demo_settings(settings):
    return settings.model_copy(
        update={
            "platform_users_collection": "demo_users",
            "platform_candidates_collection": "demo_candidates",
            "platform_companies_collection": "demo_companies",
            "platform_offers_collection": "demo_offers",
        }
    )


def test_offer_projection_maps_contract_company_and_city():
    doc = project_offer(OFFER, COMPANY)
    assert doc.contract == "PFE"
    assert doc.requirements == ["ReactJS", "Git"]
    assert doc.company.name == "Atlas Soft" and doc.company.city == "sfax"
    assert len(doc.version) == 64 and len(doc.company_version) == 64


def test_city_is_last_known_city_in_text():
    assert city_of("Route de Tunis km 4, Sfax") == "sfax"
    assert city_of("Avenue Habib Bourguiba, Sousse 4000") == "sousse"
    assert city_of("Gabès") == "gabes"
    assert city_of("Somewhere else") is None


def test_offer_version_changes_only_with_projected_content():
    base = project_offer(OFFER, COMPANY).version
    assert project_offer({**OFFER, "views": 42}, COMPANY).version == base
    assert (
        project_offer({**OFFER, "description": OFFER["description"] + " Docker."}, COMPANY).version
        != base
    )


@pytest.mark.parametrize(
    "offer,company",
    [
        ({**OFFER, "status": "CLOSED"}, COMPANY),
        ({**OFFER, "type": "FREELANCE"}, COMPANY),
        (OFFER, {**COMPANY, "isVerified": False}),
        (OFFER, None),
        ({**OFFER, "companyId": "other"}, COMPANY),
    ],
)
def test_ineligible_offers_project_to_none(offer, company):
    assert project_offer(offer, company) is None


def test_projection_tolerates_odd_shapes():
    assert project_offer(
        {**OFFER, "requiredSkills": {"Python": "advanced"}}, COMPANY
    ).requirements == ["Python"]
    assert project_offer({**OFFER, "requiredSkills": None}, COMPANY).requirements == []
    assert project_offer(
        {**OFFER, "requiredSkills": [{"name": "Docker"}, 42, {"level": 3}]}, COMPANY
    ).requirements == ["Docker"]
    assert project_offer({**OFFER, "requiredSkills": "Python, SQL; Git"}, COMPANY).requirements == [
        "Python",
        "SQL",
        "Git",
    ]
    assert project_offer({k: v for k, v in OFFER.items() if k != "id"}, COMPANY) is None
    assert project_offer({**OFFER, "title": "  "}, COMPANY) is None
    assert project_candidate({**CANDIDATE, "parsedExperiences": "not a list"}, USER).statements == [
        "not a list",
        "Licence informatique",
    ]


def test_candidate_projection_excludes_identity():
    doc = project_candidate(CANDIDATE, USER)
    dumped = json.dumps(doc.model_dump())
    for secret in ("Amira", "+216", "example.test", "cv.pdf"):
        assert secret not in dumped
    assert doc.skills == ["Python", "SQL"]
    assert doc.statements == ["Stage - Orbit - Built a Django API", "Licence informatique"]
    assert doc.city == "sfax" and doc.is_new is True
    renamed = project_candidate({**CANDIDATE, "fullName": "Someone Else", "phone": "1"}, USER)
    assert renamed.version == doc.version


def test_candidate_free_text_is_scrubbed_of_identity():
    raw = {
        **CANDIDATE,
        "parsedExperiences": [
            "Amira Synthetique built a CRM; contact amira.s@example.test or +216 00 000 001, "
            "portfolio https://amira.example.test/work"
        ],
    }
    statement = project_candidate(raw, USER).statements[0]
    assert statement == "[name] built a CRM; contact [email] or [phone], portfolio [link]"
    dated = project_candidate(
        {**CANDIDATE, "parsedExperiences": ["Stage 2019 - 2021, v1.2.3"]}, USER
    )
    assert dated.statements[0] == "Stage 2019 - 2021, v1.2.3"


@pytest.mark.parametrize(
    "candidate,user",
    [
        (CANDIDATE, {**USER, "isActive": False}),
        (CANDIDATE, {**USER, "role": "RECRUITER"}),
        ({**CANDIDATE, "parsedSkills": []}, USER),
        (CANDIDATE, None),
    ],
)
def test_ineligible_candidates_project_to_none(candidate, user):
    assert project_candidate(candidate, user) is None


def test_alias_equivalence_and_implication():
    table = AliasTable.load()
    assert normalize_term("  React.JS ") == "react js"
    assert table.canonical("ReactJS") == table.canonical("React.js") == "react"
    assert table.canonical("Développement Frontend") == "frontend"
    assert table.covers("Développement Frontend", "ReactJS")
    assert not table.covers("ReactJS", "Développement Frontend")
    assert table.covers("Développement web", "ReactJS")
    assert table.covers("SQL", "PostgreSQL")
    assert not table.covers("Python", "Comptabilité")


def test_alias_extract_prefers_longest_reviewed_terms():
    table = AliasTable.load()
    text = "Junior developer with ReactJS, Node.js, développement mobile and MongoDB in Sfax."
    assert table.extract(text) == ["React", "Node.js", "Mobile development", "MongoDB"]
    assert table.extract("Accounting and accounting again") == ["Accounting"]
    assert table.extract("No known skills here") == []


async def test_seed_refuses_real_platform_collections(settings):
    with pytest.raises(ValueError, match="demo"):
        await seed_platform(None, settings)


async def test_catalog_reads_seeded_demo_collections(mongo_db, settings):
    config = demo_settings(settings)
    first = await seed_platform(mongo_db, config)
    second = await seed_platform(mongo_db, config)
    assert first == second
    seed = json.loads(SEED.read_text("utf-8"))
    verified = {c["id"] for c in seed["companies"] if c.get("isVerified")}
    expected = {
        o["id"] for o in seed["offers"] if o["status"] == "OPEN" and o["companyId"] in verified
    }
    catalog = PlatformCatalog(mongo_db, config)
    offers, skipped = await catalog.eligible_offers()
    assert {o.id for o in offers} == expected and skipped == 0
    candidates, skipped = await catalog.eligible_candidates()
    assert skipped == 0 and len(candidates) >= 30
    closed = next(o["id"] for o in seed["offers"] if o["status"] == "CLOSED")
    assert await catalog.offer(closed) is None
    sample = candidates[0]
    identity = await catalog.identity(sample.id)
    assert identity["email"].endswith("@example.test") and identity["name"]
    assert (await catalog.candidate(sample.id)).version == sample.version
