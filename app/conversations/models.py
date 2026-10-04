from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.domain import Input

Keyword = Annotated[str, Field(min_length=2, max_length=40)]


class ConversationCreate(Input):
    language: Literal["fr", "en"] = "fr"
    profile_id: str | None = Field(default=None, min_length=1, max_length=128)
    profile_version: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def profile_pair(self):
        if (self.profile_id is None) != (self.profile_version is None):
            raise ValueError("Profile ID and version must be provided together")
        return self


class TurnRequest(Input):
    expected_revision: int = Field(ge=0, strict=True)
    message: str = Field(min_length=2, max_length=2000)


class ContextDecision(Input):
    intent: Literal["advice", "matching", "clarify"]
    query: str = Field(min_length=2, max_length=350)
    keywords: list[Keyword] = Field(max_length=8)


class Answer(Input):
    text: str = Field(min_length=1, max_length=4000)
    cited_ids: list[Annotated[str, Field(min_length=1, max_length=600)]] = Field(max_length=5)
