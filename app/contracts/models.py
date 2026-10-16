from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from app.conversations.models import TurnRequest
from app.cv.models import CorrectionFact, Evidence, FieldName
from app.domain import DraftCreate, Input
from app.dossier import CVSuggestion, EvidenceRow
from app.matching.models import Identifier, OfferFilters
from app.matching.results import CandidateMatch, Match, OfferBinding, Sha256

CONTRACT_VERSION = "agent-v2"
Version = Annotated[int, Field(ge=1, strict=True)]


class ErrorDetail(Input):
    code: str


class AgentError(Input):
    detail: ErrorDetail


class ValidationIssue(BaseModel):
    loc: list[str | int]
    msg: str
    type: str


class AgentValidationError(Input):
    detail: list[ValidationIssue]


class FacadeEnvelope(Input):
    contract_version: Literal["agent-v2"] = CONTRACT_VERSION
    submitted: Literal[False] = False


class ParseReceipt(FacadeEnvelope):
    import_id: Identifier
    status: Literal["queued", "review", "accepted", "failed"]
    version: int = Field(ge=0, strict=True)
    poll_url: str
    requires_human_approval: bool
    sync_status: Literal["not_integrated"] = "not_integrated"


class GeneratePackRequest(Input):
    profile_id: Identifier
    profile_version: Version
    offer_id: Identifier
    # The platform has no version field: this is the offer's content hash.
    offer_version: Sha256
    language: Literal["fr", "en"] = "fr"


class PlatformPackDraft(DraftCreate):
    # Internal source binding enters the existing idempotency hash and history.
    offer_snapshot: OfferBinding


class PackReceipt(FacadeEnvelope):
    draft_id: Identifier
    task_id: Identifier
    status: Literal["pending", "running", "done", "failed"]
    poll_url: str
    draft_url: str
    sources: OfferBinding
    requires_human_approval: Literal[True] = True
    adaptation_format: Literal["suggestions"] = "suggestions"


class ChatRequest(TurnRequest):
    conversation_id: Identifier


class GenerationProvenance(Input):
    provider: str
    model: str
    fallback_used: bool


class ReferenceHit(BaseModel):
    # Project public provenance without requiring all fields in old saved responses.
    id: str
    version: int | None = None
    source: str
    kind: str
    labels: dict[str, str]
    descriptions: dict[str, str] = Field(default_factory=dict)
    provenance: dict[str, JsonValue]
    cited: bool


class ConversationResponse(Input):
    status: Literal[
        "answered",
        "clarification_required",
        "insufficient_knowledge",
        "matching_unavailable",
        "matches_found",
        "no_matches",
    ]
    intent: Literal["advice", "matching", "clarify"]
    query: str
    keywords: list[str] = Field(max_length=8)
    target: Literal["offers", "candidates"] | None
    text: str
    results: list[Match] = Field(max_length=5)
    references: list[ReferenceHit] = Field(max_length=5)
    submitted: Literal[False]
    routing: GenerationProvenance
    generation: GenerationProvenance | None
    knowledge_generation: JsonValue = None
    search_mode: Literal["disabled", "platform"] = "disabled"
    filters: OfferFilters = Field(default_factory=OfferFilters)
    criteria: list[str] = Field(default_factory=list)
    anonymized: bool = False
    explanation_source: Literal["model", "template"] | None = None
    stale_dropped: int = Field(default=0, ge=0)


class ChatReceipt(FacadeEnvelope):
    conversation_id: Identifier
    revision: int = Field(ge=1, strict=True)
    message: str
    created_at: datetime
    response: ConversationResponse


class AgentCapabilities(FacadeEnvelope):
    identity_integrated: bool
    database_configured: bool
    advice_configured: bool
    offer_search: Literal["disabled", "platform"]
    candidate_sourcing: Literal["disabled", "platform"]
    application_scoring: Literal["disabled", "platform"]
    profile_evaluator: Literal["disabled", "platform"]
    pack_offer_resolution: Literal["not_integrated", "platform"]
    matching_index_ready: bool
    anonymization: bool
    profile_sync: Literal["not_integrated"] = "not_integrated"
    submission_enabled: Literal[False] = False


class SourceCandidatesRequest(Input):
    """Recruiter sourcing from one of their own offers or from pasted job text."""

    offer_id: Identifier | None = None
    job_description: str | None = Field(default=None, min_length=20, max_length=8000)
    anonymized: bool = False
    language: Literal["fr", "en"] = "fr"

    @model_validator(mode="after")
    def one_source(self):
        if (self.offer_id is None) == (self.job_description is None):
            raise ValueError("Provide exactly one of offer_id or job_description")
        return self


class SourcingReceipt(FacadeEnvelope):
    target: Literal["candidates"] = "candidates"
    offer: OfferBinding | None
    criteria: list[str]
    anonymized: bool
    status: Literal["matches_found", "no_matches"]
    text: str
    results: list[CandidateMatch] = Field(max_length=5)
    explanation_source: Literal["model", "template"] | None
    generation: GenerationProvenance | None
    stale_dropped: int = Field(ge=0)


class ScoreApplicationRequest(Input):
    offer_id: Identifier
    candidate_profile_id: Identifier
    language: Literal["fr", "en"] = "fr"


class ApplicationScore(FacadeEnvelope):
    """Deterministic compat-v1 score; never changes the application's status."""

    offer: OfferBinding
    result: CandidateMatch


class CandidateRefRequest(Input):
    candidate_ref: str = Field(pattern=r"^C-[0-9A-F]{10}$")


class CandidateRefResolution(FacadeEnvelope):
    candidate_ref: str
    candidate_profile_id: Identifier


class TaskStatus(Input):
    id: Identifier
    draft_id: Identifier
    state: Literal["pending", "running", "done", "failed"]
    attempts: int = Field(ge=0)
    error_code: str | None


class CompletenessCriterion(Input):
    criterion: str
    weight: int
    points: int


class CompletenessEvaluation(Input):
    kind: Literal["completeness"]
    rubric_version: str
    score: int = Field(ge=0, le=100)
    breakdown: list[CompletenessCriterion]
    strengths: list[str]
    advice: list[str]
    warnings: list[str]


class EvaluateProfileRequest(Input):
    profile_id: Identifier
    profile_version: Version
    language: Literal["fr", "en"] = "fr"


class MarketGap(Input):
    requirement: str
    offers: int = Field(ge=0)
    of: int = Field(ge=0)


class ProfileStrength(Input):
    text: str
    quote: str


class ProfileAdvice(Input):
    text: str
    basis: Literal["missing_field", "market_gap", "wording"]


class ProfileAssessment(FacadeEnvelope):
    """§5.1 profile evaluator. Strengths quote the profile; advice states its basis."""

    profile_id: Identifier
    profile_version: Version
    language: Literal["fr", "en"]
    completeness: CompletenessEvaluation
    strengths: list[ProfileStrength] = Field(max_length=5)
    advice: list[ProfileAdvice] = Field(max_length=6)
    market_gaps: list[MarketGap] = Field(max_length=5)
    assessment_source: Literal["model", "template"]
    generation: GenerationProvenance | None
    cached: bool
    created_at: datetime


class CVFact(Input):
    field: FieldName
    value: str
    origin: Literal["pdf", "candidate_asserted"]
    evidence: Evidence | None


class CVPage(Input):
    page: int
    text: str


class CVImportView(Input):
    id: Identifier
    status: Literal["queued", "review", "accepted", "failed"]
    version: int = Field(ge=0)
    language: Literal["fr", "en"]
    sha256: str
    byte_count: int
    facts: list[CVFact] = Field(default_factory=list)
    pages: list[CVPage] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None
    profile_id: str | None = None
    provider: str | None = None
    model: str | None = None
    fallback_used: bool | None = None
    created_at: datetime
    updated_at: datetime
    requires_human_approval: bool
    evaluation: CompletenessEvaluation | None


class DossierAnalysis(Input):
    evidence: list[EvidenceRow]
    cv_suggestions: list[CVSuggestion]


class LegacySyntheticOfferBinding(Input):
    """Drafts generated before agent-v2 against the retired synthetic fixtures."""

    kind: Literal["synthetic_offer"]
    offer_id: Identifier
    offer_version: Version
    company_id: Identifier
    company_version: Version
    snapshot_sha256: str


class SourceSnapshot(Input):
    kind: Literal["candidate_supplied_text", "synthetic_offer", "platform_offer"]
    profile_id: Identifier
    profile_version: Version
    profile_sha256: str
    job_sha256: str
    company_sha256: str
    offer: (
        Annotated[OfferBinding | LegacySyntheticOfferBinding, Field(discriminator="kind")] | None
    ) = None


class DraftView(BaseModel):
    # Preserve existing public metadata while giving clients typed core fields.
    model_config = ConfigDict(extra="allow")
    id: Identifier
    version: int = Field(ge=0)
    status: Literal["queued", "draft", "approved", "failed"]
    profile_id: Identifier
    profile_version: Version
    language: Literal["fr", "en"]
    job_description: str
    company_context: str
    requires_human_approval: bool
    approval_id: str | None
    cover_letter: str | None = None
    dossier_analysis: DossierAnalysis | None = None
    source_snapshot: SourceSnapshot | None = None
    created_at: datetime
    updated_at: datetime


class EmbeddingReference(Input):
    model_fingerprint: str
    dimensions: int = Field(ge=1, strict=True)
    index_generation: str
    record_id: Identifier
    source_version: Version


class AcceptedProfileSync(Input):
    """Proposed Node PATCH envelope, not an implemented sender or auto-publication."""

    contract_version: Literal["profile-sync-v1"] = "profile-sync-v1"
    candidate_id: Identifier
    expected_platform_version: Version
    profile_id: Identifier
    accepted_profile_version: Version
    facts: list[CorrectionFact] = Field(min_length=1, max_length=40)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    embedding_reference: EmbeddingReference | None = None
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit boolean confirmation required")
        return value
