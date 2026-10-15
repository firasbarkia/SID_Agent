from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.domain import Input

Term = Annotated[str, Field(min_length=1, max_length=80)]
Identifier = Annotated[str, Field(min_length=1, max_length=128)]


class OfferFilters(Input):
    location: Term | None = None
    contract: Literal["PFE", "INTERNSHIP", "EMPLOYMENT"] | None = None
    skills: list[Term] = Field(default_factory=list, max_length=8)


class OfferBinding(Input):
    kind: Literal["synthetic_offer"] = "synthetic_offer"
    offer_id: Identifier
    offer_version: int = Field(ge=1, strict=True)
    company_id: Identifier
    company_version: int = Field(ge=1, strict=True)
    snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class SyntheticOffer(Input):
    source: Literal["synthetic"]
    id: Identifier
    version: int = Field(ge=1, strict=True)
    company_id: Identifier
    company_version: int = Field(ge=1, strict=True)
    company_context: str = Field(min_length=1, max_length=4000)
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(min_length=20, max_length=8000)
    location: Term
    contract: Literal["PFE", "INTERNSHIP", "EMPLOYMENT"]
    skills: list[Term] = Field(min_length=1, max_length=20)
    status: Literal["OPEN", "CLOSED"]
    visibility: Literal["PUBLIC", "PRIVATE"]


class SyntheticCorpus(Input):
    synthetic: Literal[True]
    version: Identifier
    offers: list[SyntheticOffer] = Field(max_length=100)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({offer.id for offer in self.offers}) != len(self.offers):
            raise ValueError("Duplicate synthetic offer IDs")
        return self


class OfferMatch(Input):
    id: Identifier
    version: int = Field(ge=1, strict=True)
    title: str
    company_id: Identifier
    location: str
    contract: Literal["PFE", "INTERNSHIP", "EMPLOYMENT"]
    ranking_score: float = Field(ge=-1, le=1, allow_inf_nan=False)
    score_type: Literal["synthetic_cosine_similarity"] = "synthetic_cosine_similarity"
    is_compatibility_percentage: Literal[False] = False
    matched_terms: list[str]
    evidence: str
    source: Literal["synthetic"] = "synthetic"
    binding: OfferBinding


class OfferSearchResult(Input):
    mode: Literal["synthetic"] = "synthetic"
    generation: str
    filters: OfferFilters
    results: list[OfferMatch] = Field(max_length=5)


class MatchExplanation(Input):
    id: Identifier
    quote: str = Field(min_length=1, max_length=400)


class MatchAnswer(Input):
    text: str = Field(min_length=1, max_length=4000)
    evidence: list[MatchExplanation] = Field(min_length=1, max_length=5)
