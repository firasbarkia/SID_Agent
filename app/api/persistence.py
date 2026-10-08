from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request

from app.db.store import Store, ownership, public, require_document
from app.domain import (
    ApproveDraft,
    ConfirmVersion,
    DraftCreate,
    DraftEdit,
    ProfileCreate,
    ProfileEdit,
)
from app.identity import Candidate

router = APIRouter(prefix="/api/v1", tags=["Persistent candidate workspace"])


def get_store(request: Request) -> Store:
    if request.app.state.store is None:
        raise HTTPException(503, detail={"code": "mongodb_not_configured"})
    return request.app.state.store


Database = Annotated[Store, Depends(get_store)]
IdempotencyKey = Annotated[str, Header(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")]


@router.post("/profiles", status_code=201)
async def create_profile(
    payload: ProfileCreate, principal: Candidate, store: Database, request: Request
):
    if payload.external_profile_id:
        await request.app.state.platform.owned_candidate(principal, payload.external_profile_id)
    return public(await store.create_profile(principal, payload))


@router.get("/profiles/{profile_id}")
async def read_profile(profile_id: str, principal: Candidate, store: Database):
    return public(await store.profile(principal, profile_id))


@router.put("/profiles/{profile_id}")
async def edit_profile(
    profile_id: str, payload: ProfileEdit, principal: Candidate, store: Database
):
    return public(await store.edit_profile(principal, profile_id, payload))


@router.get("/profiles/{profile_id}/versions/{version}")
async def profile_version(
    profile_id: str, version: Annotated[int, Path(ge=1)], principal: Candidate, store: Database
):
    await store.profile(principal, profile_id)
    return public(
        await require_document(
            store.db.sid_profile_versions,
            {"profile_id": profile_id, "version": version, **ownership(principal)},
        )
    )


@router.post("/profiles/{profile_id}/accept")
async def accept_profile(
    profile_id: str, payload: ConfirmVersion, principal: Candidate, store: Database
):
    return public(await store.accept_profile(principal, profile_id, payload.expected_version))


@router.post("/application-drafts", status_code=202)
async def request_draft(
    payload: DraftCreate, principal: Candidate, store: Database, idempotency_key: IdempotencyKey
):
    return await store.request_draft(principal, payload, idempotency_key)


@router.get("/application-drafts/{draft_id}")
async def read_draft(draft_id: str, principal: Candidate, store: Database):
    return public(await store.draft(principal, draft_id))


@router.post("/application-dossiers", status_code=202)
async def request_dossier(
    payload: DraftCreate, principal: Candidate, store: Database, idempotency_key: IdempotencyKey
):
    """Generate a review dossier from candidate-supplied job/company text."""
    return await store.request_draft(principal, payload, idempotency_key, dossier=True)


@router.put("/application-drafts/{draft_id}")
async def edit_draft(draft_id: str, payload: DraftEdit, principal: Candidate, store: Database):
    return public(await store.edit_draft(principal, draft_id, payload))


@router.get("/application-drafts/{draft_id}/versions/{version}")
async def draft_version(
    draft_id: str, version: Annotated[int, Path(ge=1)], principal: Candidate, store: Database
):
    await store.draft(principal, draft_id)
    return public(
        await require_document(
            store.db.sid_draft_versions,
            {"draft_id": draft_id, "version": version, **ownership(principal)},
        )
    )


@router.post("/application-drafts/{draft_id}/approvals", status_code=201)
async def approve_draft(
    draft_id: str, payload: ApproveDraft, principal: Candidate, store: Database
):
    return public(await store.approve_draft(principal, draft_id, payload))


@router.get("/tasks/{task_id}")
async def read_task(task_id: str, principal: Candidate, store: Database):
    task = await require_document(store.db.sid_tasks, {"_id": task_id, **ownership(principal)})
    return {
        "id": task["_id"],
        "draft_id": task["draft_id"],
        "state": task["state"],
        "attempts": task["attempts"],
        "error_code": task.get("error_code"),
    }
