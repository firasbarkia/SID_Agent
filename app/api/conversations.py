from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.ai.errors import (
    ProviderRefused,
    ProviderRequestRejected,
    ProvidersUnavailable,
    ServiceBusy,
)
from app.api.persistence import Database, IdempotencyKey
from app.conversations.models import ConversationCreate, TurnRequest
from app.conversations.service import ConversationService
from app.conversations.store import ConversationStore, conversation_view, turn_view
from app.domain import DomainError
from app.identity import Principal, get_principal

router = APIRouter(prefix="/api/v1/conversations", tags=["Conversation pipeline"])


async def conversation_user(principal: Annotated[Principal, Depends(get_principal)]):
    if principal.role not in {"CANDIDATE", "RECRUITER"}:
        raise DomainError(403, "conversation_role_required")
    return principal


User = Annotated[Principal, Depends(conversation_user)]


@router.post("", status_code=201)
async def create(payload: ConversationCreate, principal: User, store: Database):
    return conversation_view(await ConversationStore(store.db).create(principal, payload))


@router.get("/{conversation_id}")
async def read(conversation_id: str, principal: User, store: Database):
    conversations = ConversationStore(store.db)
    head = await conversations.get(principal, conversation_id)
    return {
        **conversation_view(head),
        "turns": [
            turn_view(turn) for turn in await conversations.turns(principal, conversation_id)
        ],
    }


@router.post("/{conversation_id}/turns")
async def respond(
    conversation_id: str,
    payload: TurnRequest,
    principal: User,
    store: Database,
    request: Request,
    idempotency_key: IdempotencyKey,
):
    service = ConversationService(
        store.db,
        request.app.state.ai_gateway,
        request.app.state.knowledge_search,
        request.app.state.platform,
    )
    try:
        return await service.respond(principal, conversation_id, payload, idempotency_key)
    except (ProvidersUnavailable, ServiceBusy, TimeoutError) as exc:
        raise HTTPException(503, detail={"code": "conversation_ai_unavailable"}) from exc
    except ProviderRefused as exc:
        raise HTTPException(422, detail={"code": "generation_blocked"}) from exc
    except ProviderRequestRejected as exc:
        raise HTTPException(502, detail={"code": "provider_request_rejected"}) from exc


@router.delete("/{conversation_id}", status_code=204)
async def delete(conversation_id: str, principal: User, store: Database):
    await ConversationStore(store.db).delete(principal, conversation_id)
    return Response(status_code=204)
