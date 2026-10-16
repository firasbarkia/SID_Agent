from typing import Annotated, Literal

from pydantic import Field

from app.domain import Input

Term = Annotated[str, Field(min_length=1, max_length=80)]
Identifier = Annotated[str, Field(min_length=1, max_length=128)]


class OfferFilters(Input):
    location: Term | None = None
    contract: Literal["PFE", "INTERNSHIP", "EMPLOYMENT"] | None = None
    skills: list[Term] = Field(default_factory=list, max_length=8)


class MatchExplanation(Input):
    id: Identifier
    quote: str = Field(min_length=1, max_length=400)
    reason: str = Field(min_length=1, max_length=400)


class MatchAnswer(Input):
    text: str = Field(min_length=1, max_length=4000)
    evidence: list[MatchExplanation] = Field(min_length=1, max_length=5)
