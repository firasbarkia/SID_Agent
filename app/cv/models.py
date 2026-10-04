import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain import DomainError

FieldName = Literal[
    "name",
    "email",
    "phone",
    "location",
    "headline",
    "skill",
    "education",
    "experience",
    "project",
    "language",
]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Evidence(Input):
    page: int = Field(ge=1, le=10, strict=True)
    quote: str = Field(min_length=1, max_length=400)


class ExtractedFact(Input):
    field: FieldName
    value: str = Field(min_length=1, max_length=240)
    evidence: Evidence


class Extraction(Input):
    facts: list[ExtractedFact] = Field(max_length=40)


class CorrectionFact(Input):
    field: FieldName
    value: str = Field(min_length=1, max_length=240)


class Corrections(Input):
    expected_version: int = Field(ge=1, strict=True)
    facts: list[CorrectionFact] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def unique(self):
        pairs = [(f.field, normalized(f.value)) for f in self.facts]
        if len(set(pairs)) != len(pairs):
            raise ValueError("Duplicate facts")
        return self


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def grounded(extraction, pages):
    facts, warnings, seen = [], [], set()
    for fact in extraction.facts:
        page = next((p["text"] for p in pages if p["page"] == fact.evidence.page), None)
        if (
            page is None
            or not re.search(
                (r"(?<!\w)" if fact.evidence.quote[0].isalnum() else "")
                + re.escape(fact.evidence.quote)
                + (r"(?!\w)" if fact.evidence.quote[-1].isalnum() else ""),
                page,
            )
            or not re.search(
                r"(?<!\w)" + re.escape(normalized(fact.value)) + r"(?!\w)",
                normalized(fact.evidence.quote),
            )
        ):
            warnings.append("unsupported_fact_removed")
            continue
        pair = fact.field, normalized(fact.value)
        if pair not in seen:
            facts.append({**fact.model_dump(), "origin": "pdf"})
            seen.add(pair)
    return facts, sorted(set(warnings))


SINGULAR = {"name", "email", "phone", "location", "headline"}


def conflicts(facts):
    return sorted(
        field
        for field in SINGULAR
        if len({normalized(f["value"]) for f in facts if f["field"] == field}) > 1
    )


def review_warnings(facts):
    result = [f"conflicting_{field}" for field in conflicts(facts)]
    if any(re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", f["value"]) for f in facts):
        result.append("ambiguous_date_review_required")
    return result


def source_warnings(pages):
    # Review aid only, not a guarantee that every injection is detected.
    pattern = r"ignore.{0,40}instructions|ignore.{0,40}rules|ignor.{0,40}instructions"
    if any(re.search(pattern, p["text"], re.IGNORECASE) for p in pages):
        return ["potential_instruction_in_source"]
    return []


def profile_content(facts):
    text = "\n".join(f"{f['field']}: {f['value']}" for f in facts)
    if not 20 <= len(text) <= 12000:
        raise DomainError(422, "profile_content_size_invalid")
    return text


def evaluate(facts):
    # Presence only: this is neither employability nor job compatibility.
    present = {f["field"] for f in facts} - set(conflicts(facts))
    criteria = [
        ("name", 10, {"name"}, "Add your name."),
        ("contact", 10, {"email", "phone"}, "Add one contact method."),
        ("location", 10, {"location"}, "Add your preferred/current location."),
        ("headline", 10, {"headline"}, "Describe your target role or internship."),
        ("skills", 20, {"skill"}, "List skills you can demonstrate."),
        ("education", 20, {"education"}, "Add your education or training."),
        (
            "experience_or_projects",
            20,
            {"experience", "project"},
            "Add a work experience or a relevant project.",
        ),
    ]
    breakdown = [
        {"criterion": name, "weight": weight, "points": weight if present & fields else 0}
        for name, weight, fields, _ in criteria
    ]
    return {
        "kind": "completeness",
        "rubric_version": "1",
        "score": sum(row["points"] for row in breakdown),
        "breakdown": breakdown,
        "strengths": [row["criterion"] for row in breakdown if row["points"]],
        "advice": [advice for _, _, fields, advice in criteria if not present & fields],
        "warnings": review_warnings(facts),
    }
