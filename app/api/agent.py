"""Typed facade. Existing domain workflows retain ownership, leases and review gates."""

from contextlib import suppress
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response, Security
from fastapi.security import HTTPBearer
from pymongo.errors import PyMongoError

from app.api.conversations import User, respond
from app.api.cv import upload_cv
from app.api.drafts import require_service_key
from app.api.persistence import Database, IdempotencyKey
from app.contracts.models import (
    AgentCapabilities,
    AgentError,
    AgentValidationError,
    ApplicationScore,
    CandidateRefRequest,
    CandidateRefResolution,
    ChatReceipt,
    ChatRequest,
    EvaluateProfileRequest,
    GeneratePackRequest,
    PackReceipt,
    ParseReceipt,
    PlatformPackDraft,
    ProfileAssessment,
    ScoreApplicationRequest,
    SourceCandidatesRequest,
    SourcingReceipt,
)
from app.conversations.models import TurnRequest
from app.domain import DomainError
from app.identity import Candidate, Recruiter
from app.matching.engine import MatchEngine, resolve_platform_offer
from app.matching.evaluator import assess
from app.matching.explain import explain, no_matches_text
from app.matching.models import OfferFilters

# Extraction documents the proposed transport. get_principal performs actual
# verification via the platform callback; this dependency never validates a JWT.
platform_token = HTTPBearer(auto_error=False, scheme_name="PlatformUserToken")
security = [{"APIKeyHeader": [], "PlatformUserToken": []}]
errors = {
    code: {"model": AgentError} for code in (400, 401, 403, 404, 409, 410, 413, 415, 502, 503)
}
errors[422] = {"model": AgentError | AgentValidationError}
router = APIRouter(
    prefix="/agent",
    tags=["Agent facade"],
    dependencies=[Depends(require_service_key), Security(platform_token)],
    responses=errors,
)


def document_facade_security(application):
    original = application.openapi

    def openapi():
        schema = original()
        # FastAPI merges optional extraction dependencies as alternatives. Every
        # facade POST actually requires both service and verified user identity.
        for path, item in schema["paths"].items():
            if path.startswith("/agent/") and "post" in item:
                item["post"]["security"] = security
        schema["paths"]["/agent/capabilities"]["get"]["security"] = [{"APIKeyHeader": []}]
        return schema

    application.openapi = openapi


@router.post(
    "/parse-cv",
    status_code=202,
    response_model=ParseReceipt,
    openapi_extra={
        "security": security,
        "requestBody": {
            "required": True,
            "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}},
        },
    },
)
async def parse_cv(
    request: Request,
    response: Response,
    principal: Candidate,
    store: Database,
    idempotency_key: IdempotencyKey,
    language: Literal["fr", "en"] = "fr",
):
    head = await upload_cv(request, principal, store, idempotency_key, language)
    poll_url = f"/api/v1/cv-imports/{quote(head['id'], safe='')}"
    response.headers["Location"] = poll_url
    return ParseReceipt(
        import_id=head["id"],
        status=head["status"],
        version=head["version"],
        requires_human_approval=head["requires_human_approval"],
        poll_url=poll_url,
    )


def pack_job_text(offer, language):
    job = f"{offer.title}\n\n{offer.description}"
    if offer.requirements:
        label = "Compétences requises : " if language == "fr" else "Required skills: "
        job += f"\n\n{label}{', '.join(offer.requirements)}"
    return job[:8000]


@router.post(
    "/generate-pack",
    status_code=202,
    response_model=PackReceipt,
    openapi_extra={"security": security},
)
async def generate_pack(
    payload: GeneratePackRequest,
    request: Request,
    response: Response,
    principal: Candidate,
    store: Database,
    idempotency_key: IdempotencyKey,
):
    catalog = getattr(request.app.state, "catalog", None)
    if catalog is None:
        raise DomainError(503, "platform_data_unavailable")
    # Job and company text are loaded server-side from the exact offer version.
    offer = await resolve_platform_offer(
        catalog, principal, payload.offer_id, payload.offer_version
    )
    binding = MatchEngine.binding(offer)
    job = pack_job_text(offer, payload.language)
    company = offer.company
    city = (company.city or "").title()
    context = ". ".join(
        part for part in (company.name, company.sector, company.description, city) if part
    )
    draft = PlatformPackDraft(
        profile_id=payload.profile_id,
        profile_version=payload.profile_version,
        language=payload.language,
        job_description=job,
        company_context=context[:4000],
        offer_snapshot=binding,
    )
    receipt = await store.request_draft(principal, draft, idempotency_key, dossier=True)
    poll_url = f"/api/v1/tasks/{quote(receipt['task_id'], safe='')}"
    response.headers["Location"] = poll_url
    return PackReceipt(
        **receipt,
        sources=binding,
        poll_url=poll_url,
        draft_url=f"/api/v1/application-drafts/{quote(receipt['draft_id'], safe='')}",
    )


@router.post("/chat", response_model=ChatReceipt, openapi_extra={"security": security})
async def chat(
    payload: ChatRequest,
    principal: User,
    store: Database,
    request: Request,
    idempotency_key: IdempotencyKey,
):
    turn = await respond(
        payload.conversation_id,
        TurnRequest(
            expected_revision=payload.expected_revision,
            message=payload.message,
            anonymized=payload.anonymized,
        ),
        principal,
        store,
        request,
        idempotency_key,
    )
    return ChatReceipt(conversation_id=payload.conversation_id, **turn)


@router.get("/capabilities", response_model=AgentCapabilities)
async def capabilities(request: Request):
    state = request.app.state
    engine = getattr(state, "match_engine", None)
    ready = False
    if engine is not None:
        with suppress(PyMongoError):
            ready = all(kind["ready"] for kind in (await engine.index.status()).values())
    enabled = "platform" if engine is not None else "disabled"
    return AgentCapabilities(
        identity_integrated=state.identity_resolver is not None,
        database_configured=state.store is not None,
        advice_configured=state.knowledge_search is not None,
        offer_search=enabled,
        candidate_sourcing=enabled,
        application_scoring=enabled,
        profile_evaluator=enabled,
        pack_offer_resolution=(
            "platform" if getattr(state, "catalog", None) is not None else "not_integrated"
        ),
        matching_index_ready=ready,
        anonymization=engine is not None and engine.anonymizer is not None,
    )


def engine_of(request):
    engine = getattr(request.app.state, "match_engine", None)
    if engine is None:
        raise DomainError(503, "matching_unavailable")
    return engine


@router.post(
    "/source-candidates", response_model=SourcingReceipt, openapi_extra={"security": security}
)
async def source_candidates(
    payload: SourceCandidatesRequest, principal: Recruiter, request: Request
):
    engine = engine_of(request)
    offer = None
    if payload.offer_id is not None:
        offer = await engine.recruiter_offer(principal, payload.offer_id)
        query = f"{offer.title}. {', '.join(offer.requirements)}"
        criteria, context = offer.requirements, offer.description
    else:
        # Criteria come from reviewed skill names only, never from model inference.
        query = payload.job_description
        criteria, context = engine.rubric.aliases.extract(query), ""
    found = await engine.search_candidates(
        principal,
        query,
        criteria=criteria,
        context=context,
        keywords=[],
        filters=OfferFilters(),
        anonymized=payload.anonymized,
    )
    receipt = {
        "offer": engine.binding(offer) if offer else None,
        "criteria": found.criteria,
        "anonymized": payload.anonymized,
        "stale_dropped": found.stale_dropped,
    }
    if not found.results:
        return SourcingReceipt(
            **receipt,
            status="no_matches",
            text=no_matches_text("candidates", payload.language),
            results=[],
            explanation_source=None,
            generation=None,
        )
    explained = await explain(
        request.app.state.ai_gateway,
        found.results,
        {
            "role": principal.role,
            "language": payload.language,
            "message": query[:2000],
            "requirements": found.criteria,
        },
        payload.language,
        "candidates",
    )
    return SourcingReceipt(
        **receipt,
        status="matches_found",
        text=explained["text"],
        results=explained["results"],
        explanation_source=explained["explanation_source"],
        generation=explained["generation"],
    )


@router.post(
    "/score-application", response_model=ApplicationScore, openapi_extra={"security": security}
)
async def score_application(
    payload: ScoreApplicationRequest, principal: Recruiter, request: Request
):
    binding, result = await engine_of(request).score(
        principal, payload.offer_id, payload.candidate_profile_id, language=payload.language
    )
    return ApplicationScore(offer=binding, result=result)


@router.post(
    "/candidate-refs/resolve",
    response_model=CandidateRefResolution,
    openapi_extra={"security": security},
)
async def resolve_candidate_ref(
    payload: CandidateRefRequest, principal: Recruiter, request: Request
):
    candidate_id = await engine_of(request).resolve_candidate_ref(principal, payload.candidate_ref)
    return CandidateRefResolution(
        candidate_ref=payload.candidate_ref, candidate_profile_id=candidate_id
    )


@router.post(
    "/evaluate-profile", response_model=ProfileAssessment, openapi_extra={"security": security}
)
async def evaluate_profile(
    payload: EvaluateProfileRequest, principal: Candidate, store: Database, request: Request
):
    result = await assess(
        store.db,
        request.app.state.ai_gateway,
        getattr(request.app.state, "match_engine", None),
        principal,
        payload,
    )
    return ProfileAssessment(**result)
