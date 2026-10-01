import hashlib
import json
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Source = Literal["esco", "rome", "onet", "geonames"]
Kind = Literal["skill", "occupation", "location"]


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_url: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    snapshot: str = Field(min_length=1)
    file: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    license: str = Field(min_length=1)
    license_url: str = ""
    attribution: str = Field(min_length=1)
    edition_verified: bool = True


class KnowledgeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Source
    kind: Kind
    source_id: str = Field(min_length=1, max_length=500)
    labels: dict[str, str]
    descriptions: dict[str, str] = Field(default_factory=dict)
    aliases: list[str] = Field(default_factory=list)
    attributes: dict = Field(default_factory=dict)
    provenance: Provenance

    @field_validator("labels")
    @classmethod
    def valid_labels(cls, value):
        if not value or any(not isinstance(v, str) or not v.strip() for v in value.values()):
            raise ValueError("At least one non-empty label is required")
        return value

    @property
    def id(self):
        # No merging by label across taxonomies, even when names match.
        return f"{self.source}:{self.kind}:{self.source_id}"

    @property
    def content_hash(self):
        return digest(self.model_dump())

    def search_text(self):
        parts = [*self.labels.values(), *self.aliases, *self.descriptions.values()]
        return "\n".join(dict.fromkeys(p.strip() for p in parts if p.strip()))


def exact_skill_name(value: str) -> str:
    """Reviewed equivalence only; broader skills are never synonyms."""
    key = unicodedata.normalize("NFKC", value).strip().casefold()
    return {"reactjs": "react", "react.js": "react", "python 3": "python3"}.get(key, key)
