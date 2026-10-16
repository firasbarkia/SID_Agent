"""Internal aggregate queue status; never returns source text or task identities."""

from fastapi import APIRouter, Depends, Request

from app.api.drafts import require_service_key
from app.api.persistence import Database
from app.db.connection import server_time

router = APIRouter(
    prefix="/api/v1/operations", tags=["Operations"], dependencies=[Depends(require_service_key)]
)


@router.get("/status")
async def operations_status(request: Request, store: Database):
    now = await server_time(store.db)
    queues = {}
    for name in ("sid_tasks", "sid_cv_tasks", "sid_index_jobs", "sid_outbox"):
        rows = await (
            await store.db[name].aggregate(
                [{"$group": {"_id": "$state", "count": {"$sum": 1}}}], maxTimeMS=3000
            )
        ).to_list()
        counts = {state: 0 for state in ("pending", "running", "done", "failed")}
        for row in rows:
            key = row["_id"] if row["_id"] in counts else "other"
            counts[key] = counts.get(key, 0) + row["count"]
        expired = await store.db[name].count_documents(
            {"state": "running", "lease_until": {"$lte": now}}, maxTimeMS=3000
        )
        queues[name] = {"counts": counts, "expired_leases": expired}
    return {
        "observed_at": now,
        "queues": queues,
        "providers": await request.app.state.ai_gateway.status(),
        "identity_integrated": request.app.state.identity_resolver is not None,
        "knowledge_configured": request.app.state.knowledge_search is not None,
        "matching_enabled": False,
        "submission_enabled": False,
    }


matching_router = APIRouter(
    prefix="/api/v1/matching", tags=["Operations"], dependencies=[Depends(require_service_key)]
)


@matching_router.get("/status")
async def matching_status(request: Request):
    """Index readiness and sync state; record contents are never returned."""
    engine = getattr(request.app.state, "match_engine", None)
    if engine is None:
        return {"configured": False}
    return {"configured": True, **await engine.index.status()}
