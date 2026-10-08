from datetime import timedelta

import httpx
import pytest

from app.ai.gateway import AIGateway
from app.api.persistence import get_store
from app.db.connection import server_time
from app.db.store import Store
from app.main import create_app


@pytest.mark.mongodb
async def test_operations_is_internal_and_reports_aggregates_only(mongo_db, settings):
    now = await server_time(mongo_db)
    await mongo_db.sid_cv_tasks.insert_one(
        {
            "_id": "private-task",
            "owner_id": "private-owner",
            "state": "running",
            "lease_until": now - timedelta(seconds=1),
            "prompt": "private text",
        }
    )
    app = create_app(settings)
    app.dependency_overrides[get_store] = lambda: Store(mongo_db)
    app.state.ai_gateway = AIGateway([], settings)
    app.state.knowledge_search = None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/v1/operations/status")).status_code == 401
        result = await client.get(
            "/api/v1/operations/status", headers={"X-Service-Key": "test-service-key"}
        )
    assert result.status_code == 200
    data = result.json()
    assert data["queues"]["sid_cv_tasks"]["counts"]["running"] == 1
    assert data["queues"]["sid_cv_tasks"]["expired_leases"] == 1
    assert not data["matching_enabled"] and not data["submission_enabled"]
    assert not data["identity_integrated"]
    assert "private" not in result.text and "test-service-key" not in result.text
