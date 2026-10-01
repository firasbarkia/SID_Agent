import asyncio

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.drafts import require_service_key
from app.knowledge.models import Kind, Source

router = APIRouter(prefix="/api/v1/knowledge", dependencies=[Depends(require_service_key)])


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=2, max_length=500)
    limit: int = Field(default=5, ge=1, le=10, strict=True)
    source: Source | None = None
    kind: Kind | None = None


@router.post("/search", tags=["Knowledge base"])
async def search(payload: SearchRequest, request: Request):
    service = request.app.state.knowledge_search
    if service is None:
        raise HTTPException(503, detail={"code": "knowledge_not_configured"})
    try:
        async with asyncio.timeout(30):
            return await service.search(**payload.model_dump())
    except RuntimeError as exc:
        raise HTTPException(
            503, detail={"code": "knowledge_busy"}, headers={"Retry-After": "1"}
        ) from exc
    except ValueError as exc:
        if "Query exceeds" in str(exc):
            raise HTTPException(422, detail={"code": "query_token_limit"}) from exc
        raise HTTPException(503, detail={"code": "knowledge_index_not_ready"}) from exc
    except (httpx.HTTPError, TimeoutError) as exc:
        raise HTTPException(
            503, detail={"code": "knowledge_unavailable"}, headers={"Retry-After": "5"}
        ) from exc


@router.get("/status", tags=["Knowledge base"])
async def status(request: Request):
    service = request.app.state.knowledge_search
    if service is None:
        raise HTTPException(503, detail={"code": "knowledge_not_configured"})
    try:
        generation = await service.store.active(service.embedder.fingerprint)
        await service.index.verify(generation)
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(503, detail={"code": "knowledge_index_not_ready"}) from exc
    return {
        "status": "ready",
        "generation": generation,
        "dimensions": service.embedder.dimensions,
        "fingerprint": service.embedder.fingerprint,
    }
