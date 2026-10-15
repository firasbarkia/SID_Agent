"""Typed facade. Existing domain workflows retain ownership, leases and review gates."""

from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response, Security
from fastapi.security import HTTPBearer

from app.api.conversations import User, respond
from app.api.cv import upload_cv
from app.api.drafts import require_service_key
from app.api.persistence import Database, IdempotencyKey
from app.contracts.models import (
    AgentCapabilities,
    AgentError,
    AgentValidationError,
    ChatReceipt,
    ChatRequest,
    GeneratePackRequest,
    PackReceipt,
    ParseReceipt,
    SyntheticPackDraft,
)
from app.conversations.models import TurnRequest
from app.domain import DomainError
from app.identity import Candidate

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
        # FastAPI merges optional extraction dependencies as alternatives. These
        # three operations actually require both service and verified user identity.
        for path in ("/agent/parse-cv", "/agent/generate-pack", "/agent/chat"):
            schema["paths"][path]["post"]["security"] = security
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
    catalog = request.app.state.synthetic_offer_search
    if catalog is None:
        raise DomainError(503, "canonical_offers_not_integrated")
    offer = catalog.resolve(principal, payload.offer_id, payload.offer_version)
    binding = catalog.binding(offer)
    draft = SyntheticPackDraft(
        profile_id=payload.profile_id,
        profile_version=payload.profile_version,
        language=payload.language,
        job_description=offer.description,
        company_context=offer.company_context,
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
        TurnRequest(expected_revision=payload.expected_revision, message=payload.message),
        principal,
        store,
        request,
        idempotency_key,
    )
    return ChatReceipt(conversation_id=payload.conversation_id, **turn)


@router.get("/capabilities", response_model=AgentCapabilities)
async def capabilities(request: Request):
    return AgentCapabilities(
        identity_integrated=request.app.state.identity_resolver is not None,
        database_configured=request.app.state.store is not None,
        advice_configured=request.app.state.knowledge_search is not None,
        offer_search="synthetic" if request.app.state.synthetic_offer_search else "disabled",
        pack_offer_resolution=(
            "synthetic" if request.app.state.synthetic_offer_search else "not_integrated"
        ),
    )
