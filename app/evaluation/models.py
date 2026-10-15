import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from app.cv.models import CorrectionFact
from app.domain import Input

Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Kind = Literal["cv", "dossier", "retrieval", "memory"]


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class Case(Input):
    id: str = Field(min_length=1, max_length=100)
    kind: Kind
    split: Literal["development", "held_out"]
    language: Literal["en", "fr"]
    tags: list[str] = Field(min_length=1)
    inputs: dict[str, Any]
    expected: dict[str, Any]

    @model_validator(mode="after")
    def case_contract(self):
        inputs = {
            "cv": ("pages",),
            "dossier": ("candidate", "job", "company"),
            "retrieval": ("query", "references"),
            "memory": ("message", "previous"),
        }
        expected = {
            "cv": "facts",
            "dossier": "assessments",
            "retrieval": "relevant_ids",
            "memory": "forbidden_keywords",
        }
        if (
            not set(inputs[self.kind]) <= set(self.inputs)
            or expected[self.kind] not in self.expected
        ):
            raise ValueError("Missing workflow input or reference expectation")
        if self.expected.get("outcome", "success") not in {"success", "refused", "unavailable"}:
            raise ValueError("Invalid expected outcome")
        if self.kind == "cv":
            if not isinstance(self.expected["facts"], list):
                raise ValueError("Invalid reference facts")
            for fact in self.expected["facts"]:
                CorrectionFact.model_validate(fact)
            if not isinstance(self.inputs["pages"], list) or any(
                not isinstance(p, dict)
                or not isinstance(p.get("page"), int)
                or not isinstance(p.get("text"), str)
                for p in self.inputs["pages"]
            ):
                raise ValueError("Invalid CV pages")
        elif self.kind == "dossier":
            if not all(isinstance(self.inputs[k], str) for k in inputs[self.kind]):
                raise ValueError("Invalid dossier sources")
            assessments = self.expected["assessments"]
            if not isinstance(assessments, dict) or any(
                not isinstance(v, str) or v not in {"supported", "not_evidenced"}
                for v in assessments.values()
            ):
                raise ValueError("Invalid reference assessments")
            if not isinstance(self.expected.get("forbidden_letter_phrases", []), list) or any(
                not isinstance(p, str) for p in self.expected.get("forbidden_letter_phrases", [])
            ):
                raise ValueError("Invalid forbidden phrases")
        elif self.kind == "retrieval":
            if not isinstance(self.inputs["references"], list) or any(
                not isinstance(r, dict)
                or not isinstance(r.get("id"), str)
                or not isinstance(r.get("text"), str)
                for r in self.inputs["references"]
            ):
                raise ValueError("Invalid retrieval references")
            if not isinstance(self.expected["relevant_ids"], list) or any(
                not isinstance(p, str) for p in self.expected["relevant_ids"]
            ):
                raise ValueError("Invalid relevant IDs")
            if not set(self.expected["relevant_ids"]) <= {
                r["id"] for r in self.inputs["references"]
            }:
                raise ValueError("Relevant reference IDs must be in the supplied context")
        else:
            if (
                not isinstance(self.inputs["message"], str)
                or not isinstance(self.inputs["previous"], list)
                or any(not isinstance(k, str) for k in self.inputs["previous"])
            ):
                raise ValueError("Invalid memory inputs")
            if not isinstance(self.expected["forbidden_keywords"], list) or any(
                not isinstance(k, str) for k in self.expected["forbidden_keywords"]
            ):
                raise ValueError("Invalid forbidden keywords")
        return self


class Corpus(Input):
    schema_version: Literal[1]
    label_status: Literal["provisional_synthetic"]
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self):
        if len({c.id for c in self.cases}) != len(self.cases):
            raise ValueError("Duplicate corpus case IDs")
        return self


class GenerationConfig(Input):
    max_output_tokens: int = Field(ge=1, strict=True)
    temperature: float = Field(ge=0, le=2)
    timeout_seconds: float | None = Field(default=None, gt=0)
    top_k: int | None = Field(default=None, ge=1, strict=True)
    embedding_fingerprint: str | None = None


class Provenance(Input):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    synthetic: bool = Field(strict=True)
    prompt_sha256: dict[Kind, Hash]
    reference_snapshot: str = Field(min_length=1)
    configuration: GenerationConfig


class SavedResult(Input):
    case_id: str
    case_sha256: Hash
    outcome: Literal["success", "refused", "unavailable"]
    output: dict[str, Any] | None = None

    @model_validator(mode="after")
    def output_for_success(self):
        if (self.outcome == "success") != (self.output is not None):
            raise ValueError("Only success results must include output")
        return self


class SavedRun(Input):
    schema_version: Literal[1]
    run_id: str = Field(min_length=1)
    corpus_sha256: Hash
    provenance: Provenance
    results: list[SavedResult] = Field(min_length=1)


class HumanReview(Input):
    case_id: str
    output_sha256: Hash
    reviewer: str | None = Field(default=None, min_length=1, max_length=100)
    claim_support: Literal["supported", "unsupported", "mixed"] | None = None
    letter_factuality: Literal["faithful", "unsupported", "contradictory"] | None = None
    sentence_support: Literal["supported", "unsupported", "mixed"] | None = None
    memory_privacy: Literal["safe", "identity_leak"] | None = None
    extraction_accuracy: Literal["correct", "incorrect", "mixed"] | None = None
    writing_quality: int | None = Field(default=None, ge=1, le=5, strict=True)
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def attributed_scores(self):
        scores = (
            self.claim_support,
            self.letter_factuality,
            self.sentence_support,
            self.memory_privacy,
            self.extraction_accuracy,
            self.writing_quality,
        )
        if any(s is not None for s in scores) and self.reviewer is None:
            raise ValueError("Human scores require a named reviewer")
        return self


class Reviews(Input):
    schema_version: Literal[1]
    run_sha256: Hash
    reviews: list[HumanReview]
