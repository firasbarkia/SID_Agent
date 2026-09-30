from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DomainError(Exception):
    def __init__(self, status: int, code: str):
        self.status = status
        self.code = code
        super().__init__(code)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProfileCreate(Input):
    content: str = Field(min_length=20, max_length=12000)
    external_profile_id: str | None = Field(default=None, min_length=1, max_length=128)


class ProfileEdit(Input):
    expected_version: int = Field(ge=1)
    content: str = Field(min_length=20, max_length=12000)


class ConfirmVersion(Input):
    expected_version: int = Field(ge=1)
    confirmed: bool = Field(strict=True)

    @field_validator("confirmed")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation is required")
        return value


class DraftCreate(Input):
    profile_id: str = Field(min_length=1, max_length=128)
    profile_version: int = Field(ge=1)
    job_description: str = Field(min_length=20, max_length=8000)
    company_context: str = Field(default="", max_length=4000)
    language: Literal["fr", "en"] = "fr"


class DraftEdit(Input):
    expected_version: int = Field(ge=1)
    cover_letter: str = Field(min_length=20, max_length=20000)


class ApproveDraft(ConfirmVersion):
    destination_id: str = Field(min_length=1, max_length=128)
