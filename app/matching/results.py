"""agent-v2 matching results. Scores come from the rubric; explanations from the service."""

from typing import Annotated, Literal

from pydantic import Field

from app.domain import Input
from app.matching.models import Identifier, OfferFilters
from app.matching.rubric import Compatibility

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ExplanationSource = Literal["model", "template"]


class CompanySummary(Input):
    id: Identifier
    name: str
    sector: str = ""
    city: str | None = None


class OfferBinding(Input):
    """Pins the exact platform offer and company content a result or pack used."""

    kind: Literal["platform_offer"] = "platform_offer"
    offer_id: Identifier
    offer_version: Sha256
    company_id: Identifier
    company_version: Sha256
    snapshot_sha256: Sha256


class CandidateBinding(Input):
    kind: Literal["platform_candidate"] = "platform_candidate"
    candidate_version: Sha256


class OfferMatch(Input):
    kind: Literal["offer"] = "offer"
    source: Literal["platform"] = "platform"
    id: Identifier
    version: Sha256
    title: str
    contract: Literal["PFE", "INTERNSHIP", "EMPLOYMENT"]
    company: CompanySummary
    description: str = ""
    requirements: list[str] = Field(default_factory=list)
    relevance: float = Field(ge=0, le=1)
    compatibility: Compatibility | None = None
    score: float = Field(ge=0, le=1)
    explanation: str | None = None
    explanation_source: ExplanationSource | None = None
    evidence: str | None = None
    binding: OfferBinding


class CandidateMatch(Input):
    kind: Literal["candidate"] = "candidate"
    source: Literal["platform"] = "platform"
    anonymized: bool
    candidate_id: Identifier | None = None
    candidate_ref: str | None = None
    display_name: str
    city: str | None = None
    skills: list[str]
    highlights: list[str] = Field(default_factory=list, max_length=3)
    is_new: bool = False
    # None when scoring a known candidate directly: there is no query to be relevant to.
    relevance: float | None = Field(default=None, ge=0, le=1)
    compatibility: Compatibility | None = None
    score: float = Field(ge=0, le=1)
    explanation: str | None = None
    explanation_source: ExplanationSource | None = None
    evidence: str | None = None
    binding: CandidateBinding


Match = Annotated[OfferMatch | CandidateMatch, Field(discriminator="kind")]


class ProfileView(Input):
    """A candidate's professional profile used to enrich ranking and scoring."""

    skills: list[str] = Field(default_factory=list)
    statements: list[str] = Field(default_factory=list)
    text: str = ""


class SearchResult(Input):
    mode: Literal["platform"] = "platform"
    target: Literal["offers", "candidates"]
    filters: OfferFilters
    criteria: list[str] = Field(default_factory=list)
    results: list[Match] = Field(max_length=5)
    stale_dropped: int = 0
    pool: int = 0
