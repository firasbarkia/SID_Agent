"""Read-only projection of the platform's UML records into searchable documents.

Identity fields (name, phone, email, CV URL) never enter a projection or its version
hash: they are loaded separately by `identity()` and only for display.
"""

import re
from typing import Literal

from pydantic import Field

from app.config import Settings
from app.domain import Input
from app.knowledge.models import digest
from app.matching.aliases import normalize_term
from app.matching.anonymize import scrub

CONTRACTS = {"STAGE_PFE": "PFE", "STAGE_PERIOD": "INTERNSHIP", "EMPLOI": "EMPLOYMENT"}
Contract = Literal["PFE", "INTERNSHIP", "EMPLOYMENT"]
# Tunisian governorates and major cities. Addresses usually end with the city, and
# street names often contain other cities ("Route de Tunis, Sfax"), so the last wins.
CITIES = [
    "sfax", "tunis", "sousse", "monastir", "mahdia", "nabeul", "hammamet", "bizerte",
    "gabes", "gafsa", "kairouan", "kasserine", "medenine", "djerba", "zarzis", "ariana",
    "ben arous", "manouba", "kebili", "tozeur", "tataouine", "sidi bouzid", "siliana",
    "beja", "jendouba", "le kef", "zaghouan",
]  # fmt: skip
TERM_FIELDS = ("name", "skill", "label", "title")
STATEMENT_FIELDS = (
    "title", "role", "position", "degree", "name", "company", "school", "institution",
    "field", "description", "summary", "details",
)  # fmt: skip
MAX_TERMS = 30
MAX_STATEMENTS = 40


class CompanyInfo(Input):
    id: str
    name: str
    sector: str = ""
    city: str | None = None
    description: str = ""


class OfferDoc(Input):
    id: str
    company_id: str
    title: str
    contract: Contract
    description: str
    requirements: list[str] = Field(default_factory=list)
    company: CompanyInfo
    version: str
    company_version: str


class CandidateDoc(Input):
    id: str
    user_id: str
    skills: list[str]
    statements: list[str] = Field(default_factory=list)
    city: str | None = None
    is_new: bool = False
    version: str


def _text(value, limit) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def city_of(text) -> str | None:
    folded = f" {normalize_term(text)} "
    found = [(folded.rfind(f" {city} "), city) for city in CITIES if f" {city} " in folded]
    return max(found)[1] if found else None


def terms(value) -> list[str]:
    """Skill lists arrive as strings, objects, dicts or delimited text."""
    if isinstance(value, str):
        items = re.split(r"[,;\n]", value)
    elif isinstance(value, dict):
        items = list(value)
    elif isinstance(value, list):
        items = []
        for item in value:
            if isinstance(item, dict):
                item = next((item[f] for f in TERM_FIELDS if isinstance(item.get(f), str)), "")
            items.append(item)
    else:
        items = []
    result, seen = [], set()
    for item in items:
        term = _text(item, 80)
        key = normalize_term(term)
        if key and key not in seen:
            seen.add(key)
            result.append(term)
    return result[:MAX_TERMS]


def statements(*values) -> list[str]:
    result = []
    for value in values:
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, dict):
                parts = [_text(item.get(f), 200) for f in STATEMENT_FIELDS]
                line = " - ".join(p for p in parts if p)
            else:
                line = _text(item, 300)
            if line:
                result.append(line[:300])
    return result[:MAX_STATEMENTS]


def project_company(raw) -> CompanyInfo | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
        return None
    if raw.get("isVerified") is not True:
        return None
    name = _text(raw.get("companyName"), 300)
    if not name:
        return None
    return CompanyInfo(
        id=raw["id"],
        name=name,
        sector=_text(raw.get("sector"), 200),
        city=city_of(_text(raw.get("address"), 500)),
        description=_text(raw.get("description"), 2000),
    )


def project_offer(raw, company_raw) -> OfferDoc | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
        return None
    company = project_company(company_raw)
    if (
        company is None
        or raw.get("companyId") != company.id
        or raw.get("status") != "OPEN"
        or raw.get("type") not in CONTRACTS
    ):
        return None
    title = _text(raw.get("title"), 240)
    description = _text(raw.get("description"), 8000)
    if not title or not description:
        return None
    content = {
        "id": raw["id"],
        "company_id": company.id,
        "title": title,
        "contract": CONTRACTS[raw["type"]],
        "description": description,
        "requirements": terms(raw.get("requiredSkills")),
        "company": company.model_dump(),
    }
    return OfferDoc(
        **content,
        version=digest(content),
        company_version=digest(company.model_dump()),
    )


def project_candidate(raw, user) -> CandidateDoc | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
        return None
    if (
        not isinstance(user, dict)
        or user.get("id") != raw.get("userId")
        or user.get("role") != "CANDIDATE"
        or user.get("isActive") is not True
    ):
        return None
    name = _text(raw.get("fullName"), 200)
    # Candidates sometimes write their own name or contacts into free text.
    skills = [scrub(skill, name) for skill in terms(raw.get("parsedSkills"))]
    if not skills:
        return None
    lines = statements(
        raw.get("parsedExperiences"), raw.get("parsedProjects"), raw.get("parsedEducation")
    )
    content = {
        "id": raw["id"],
        "user_id": raw["userId"],
        "skills": skills,
        "statements": [scrub(line, name) for line in lines],
        "city": city_of(_text(raw.get("city"), 200)),
        "is_new": raw.get("isNewCandidate") is True,
    }
    return CandidateDoc(**content, version=digest(content))


class PlatformCatalog:
    """Never writes. Collection names come from settings (demo uses `demo_*`)."""

    def __init__(self, db, settings: Settings):
        self.users = db[settings.platform_users_collection]
        self.candidates = db[settings.platform_candidates_collection]
        self.companies = db[settings.platform_companies_collection]
        self.offers = db[settings.platform_offers_collection]

    async def eligible_offers(self) -> tuple[list[OfferDoc], int]:
        companies = {
            row["id"]: row
            async for row in self.companies.find({"isVerified": True})
            if isinstance(row.get("id"), str)
        }
        docs, skipped = [], 0
        async for raw in self.offers.find({"status": "OPEN"}):
            doc = project_offer(raw, companies.get(raw.get("companyId")))
            if doc is None:
                # Open offers of unverified companies are ineligible, not malformed.
                skipped += raw.get("companyId") in companies
            else:
                docs.append(doc)
        return docs, skipped

    async def eligible_candidates(self) -> tuple[list[CandidateDoc], int]:
        users = {
            row["id"]: row
            async for row in self.users.find({"role": "CANDIDATE", "isActive": True})
            if isinstance(row.get("id"), str)
        }
        docs, skipped = [], 0
        async for raw in self.candidates.find({}):
            user = users.get(raw.get("userId"))
            if user is None:
                continue
            doc = project_candidate(raw, user)
            if doc is None:
                skipped += bool(raw.get("parsedSkills"))
            else:
                docs.append(doc)
        return docs, skipped

    async def offer(self, offer_id) -> OfferDoc | None:
        raw = await self.offers.find_one({"id": offer_id})
        if raw is None:
            return None
        company = await self.companies.find_one({"id": raw.get("companyId")})
        return project_offer(raw, company)

    async def candidate(self, candidate_id) -> CandidateDoc | None:
        raw = await self.candidates.find_one({"id": candidate_id})
        if raw is None:
            return None
        return project_candidate(raw, await self.users.find_one({"id": raw.get("userId")}))

    async def candidate_for_user(self, user_id) -> CandidateDoc | None:
        raw = await self.candidates.find_one({"userId": user_id})
        if raw is None:
            return None
        return project_candidate(raw, await self.users.find_one({"id": user_id}))

    async def identity(self, candidate_id) -> dict:
        raw = await self.candidates.find_one({"id": candidate_id}) or {}
        user = await self.users.find_one({"id": raw.get("userId")}) or {}
        name = _text(raw.get("fullName"), 200)
        return {
            "name": name,
            "email": _text(user.get("email"), 320),
            "phone": _text(raw.get("phone"), 64),
            "cv_url": _text(raw.get("rawCvUrl"), 2000),
            "city": city_of(_text(raw.get("city"), 200)),
        }
