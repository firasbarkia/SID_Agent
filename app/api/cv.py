from typing import Literal

from fastapi import APIRouter, Request, Response

from app.api.persistence import Database, IdempotencyKey
from app.cv.models import Corrections, evaluate
from app.cv.pdf import MAX_BYTES
from app.cv.store import CVStore, view
from app.db.store import ownership, public, require_document
from app.domain import ConfirmVersion, DomainError
from app.identity import Candidate

router = APIRouter(prefix="/api/v1", tags=["Private CV imports"])


@router.post(
    "/cv-imports",
    status_code=202,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/pdf": {"schema": {"type": "string", "format": "binary"}},
            },
        }
    },
)
async def upload_cv(
    request: Request,
    principal: Candidate,
    store: Database,
    idempotency_key: IdempotencyKey,
    language: Literal["fr", "en"] = "fr",
):
    # Stream raw application/pdf; avoid a multipart parser buffering an unbounded body.
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/pdf"
    ):
        raise DomainError(415, "pdf_content_type_required")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_BYTES:
            raise DomainError(413, "cv_file_too_large")
        data.extend(chunk)
    if not data.startswith(b"%PDF-"):
        raise DomainError(422, "invalid_pdf")
    head = await CVStore(store.db, store.max_attempts).upload(
        principal, bytes(data), idempotency_key, language
    )
    return view(head)


@router.get("/cv-imports/{import_id}")
async def get_cv(import_id: str, principal: Candidate, store: Database):
    return view(await CVStore(store.db).get(principal, import_id))


@router.put("/cv-imports/{import_id}")
async def correct_cv(import_id: str, payload: Corrections, principal: Candidate, store: Database):
    return view(await CVStore(store.db).edit(principal, import_id, payload))


@router.get("/cv-imports/{import_id}/versions/{version}")
async def cv_version(import_id: str, version: int, principal: Candidate, store: Database):
    await CVStore(store.db).get(principal, import_id)
    return public(
        await require_document(
            store.db.sid_cv_versions,
            {
                "import_id": import_id,
                "version": version,
                **ownership(principal),
            },
        )
    )


@router.post("/cv-imports/{import_id}/accept")
async def accept_cv(
    import_id: str,
    payload: ConfirmVersion,
    principal: Candidate,
    store: Database,
):
    return view(await CVStore(store.db).accept(principal, import_id, payload.expected_version))


@router.delete("/cv-imports/{import_id}", status_code=204)
async def delete_cv(import_id: str, principal: Candidate, store: Database):
    await CVStore(store.db).delete(principal, import_id)
    return Response(status_code=204)


@router.get("/profiles/{profile_id}/evaluation")
async def evaluate_profile(profile_id: str, principal: Candidate, store: Database):
    head = await store.profile(principal, profile_id)
    if not head.get("structured_data"):
        raise DomainError(409, "structured_profile_required")
    return {
        "profile_id": profile_id,
        "version": head["version"],
        "validated_version": head["validated_version"],
        **evaluate(head["structured_data"]["facts"]),
    }
