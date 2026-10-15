"""Simulation boundaries and the same executable scenarios offered in the UI."""

import os
from contextlib import asynccontextmanager

import httpx
import psutil
import pytest

from app.demo import create_demo_app, demo_settings
from examples.simulation_checks import run_checks
from scripts.simulation import saved_process, simulation_settings


def test_simulation_ignores_inherited_production_configuration(monkeypatch):
    monkeypatch.setenv("SID_DEMO_LIVE_AI", "1")
    monkeypatch.setenv("MONGODB_URI", "mongodb://production.invalid:27017")
    monkeypatch.setenv("QDRANT_URL", "https://production.invalid")
    monkeypatch.setenv("GROQ_API_KEY", "must-not-be-used")
    monkeypatch.setenv("QDRANT_API_KEY", "must-not-be-used")
    monkeypatch.setenv("ADMISSION_USER_REQUESTS_PER_MINUTE", "999")
    config = demo_settings(simulation_settings(), live=False)
    assert config.mongodb_database == "sid_demo"
    assert "127.0.0.1:27018" in config.mongodb_uri.get_secret_value()
    assert config.qdrant_url == "http://127.0.0.1:6335"
    assert config.qdrant_api_key.get_secret_value() == ""
    assert config.groq_api_key.get_secret_value() == "simulated-no-network"
    assert config.gemini_api_key.get_secret_value() == ""
    assert config.admission_user_requests_per_minute == 10


def test_stop_does_not_claim_reused_or_unrelated_process_ids():
    process = psutil.Process(os.getpid())
    record = {"pid": process.pid, "created": process.create_time(), "command": process.cmdline()}
    assert saved_process(record).pid == process.pid
    assert saved_process({**record, "created": record["created"] - 1}) is None
    assert saved_process({**record, "command": ["unrelated-service"]}) is None
    assert saved_process({}) is None


async def test_checks_refuse_live_mode_before_writing_any_data():
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json={"ai_mode": "live"})

    report = await run_checks("http://127.0.0.1", transport=httpx.MockTransport(handler))
    assert not report["passed"]
    assert requests == [("GET", "/demo/status")]
    assert "simulated mode" in report["checks"][-1]["error"]


@pytest.mark.mongodb
async def test_all_simulation_scenarios_with_real_workers(settings, mongo_db, monkeypatch):
    from test_conversations import Knowledge

    @asynccontextmanager
    async def connection(config):
        yield mongo_db

    monkeypatch.setattr("app.main.mongo_connection", connection)
    app = create_demo_app(settings, live=False)
    async with app.router.lifespan_context(app):
        app.state.knowledge_search = Knowledge()
        report = await run_checks("http://127.0.0.1", transport=httpx.ASGITransport(app=app))
    assert report["passed"], report["checks"][-1]
    assert len(report["checks"]) == 13
    assert {"/agent/chat", "/agent/parse-cv", "/agent/generate-pack"} <= {
        row["path"] for row in report["trace"]
    }
    assert await mongo_db.sid_approvals.count_documents({}) == 0
    assert await mongo_db.sid_drafts.count_documents({"status": "draft"}) == 1
