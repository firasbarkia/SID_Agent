"""Local test-auth boundary and actual persistence/worker regression checks."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.demo import DEMO_SERVICE_KEY, create_demo_app, demo_settings
from app.main import create_app


def test_demo_uses_separate_database_and_fake_credentials(settings):
    config = demo_settings(settings)
    assert config.mongodb_database == "sid_demo"
    assert config.platform_users_collection == "demo_users"
    assert config.groq_api_key.get_secret_value() == "simulated-no-network"
    assert config.gemini_api_key.get_secret_value() == ""
    assert settings.groq_api_key.get_secret_value() == "test-groq-key"
    live = demo_settings(settings, live=True)
    assert live.groq_api_key == settings.groq_api_key
    assert live.ai_account_scope == settings.ai_account_scope
    assert live.groq_requests_per_minute == settings.groq_requests_per_minute


@pytest.mark.parametrize(
    "field,value",
    [
        ("mongodb_uri", SecretStr("mongodb://remote.example.test:27017")),
        ("qdrant_url", "https://remote.example.test"),
    ],
)
def test_demo_rejects_remote_databases(settings, field, value):
    with pytest.raises(ValueError, match="localhost"):
        demo_settings(settings.model_copy(update={field: value}))


@pytest.mark.parametrize(
    "peer,host,origin",
    [
        ("192.0.2.1", "127.0.0.1", None),
        ("127.0.0.1", "remote.example.test", None),
        ("127.0.0.1", "127.0.0.1", "https://remote.example.test"),
    ],
)
async def test_demo_rejects_remote_requests_without_connecting_to_mongo(
    settings, peer, host, origin
):
    app = create_demo_app(settings, live=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(peer, 1234)),
        base_url=f"http://{host}",
    ) as client:
        result = await client.get("/demo/status", headers={"Origin": origin} if origin else {})
        assert result.status_code == 403


def test_production_factory_still_requires_login(settings):
    assert create_app(settings).state.identity_resolver is None


@asynccontextmanager
async def demo_api(settings, db, monkeypatch):
    @asynccontextmanager
    async def connection(config):
        # Replace only the connection with the disposable test database.
        yield db

    monkeypatch.setattr("app.main.mongo_connection", connection)
    app = create_demo_app(settings, live=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"X-Service-Key": DEMO_SERVICE_KEY, "X-Demo-User": "demo-candidate"},
        ) as client:
            yield client


@pytest.mark.mongodb
async def test_demo_no_login_dossier_workers_approval_and_user_isolation(
    settings, mongo_db, monkeypatch
):
    async with demo_api(settings, mongo_db, monkeypatch) as client:
        assert (await client.get("/demo/status")).json()["ai_mode"] == "simulated"
        profile = (
            await client.post(
                "/api/v1/profiles",
                json={"content": "Student with Python and SQL university projects."},
            )
        ).json()
        accepted = await client.post(
            f"/api/v1/profiles/{profile['id']}/accept",
            json={"expected_version": 1, "confirmed": True},
        )
        assert accepted.status_code == 200
        task = await client.post(
            "/api/v1/application-dossiers",
            headers={"Idempotency-Key": "demo-dossier-one"},
            json={
                "profile_id": profile["id"],
                "profile_version": 1,
                "job_description": "Python internship for a student with SQL projects.",
                "language": "en",
            },
        )
        assert task.status_code == 202
        url = f"/api/v1/application-drafts/{task.json()['draft_id']}"
        async with asyncio.timeout(15):
            while True:
                draft = (await client.get(url)).json()
                if draft["status"] != "queued":
                    break
                await asyncio.sleep(0.1)
        assert draft["status"] == "draft", draft
        assert draft["model"] == "simulated-demo"
        assert draft["dossier_analysis"]["evidence"]
        approval = await client.post(
            url + "/approvals",
            json={"expected_version": 1, "confirmed": True, "destination_id": "demo-offer"},
        )
        assert approval.status_code == 201
        client.headers["X-Demo-User"] = "demo-candidate-two"
        assert (await client.get(url)).status_code == 404
        assert (await client.get("/demo/workspace")).json()["profiles"] == []
        client.headers["X-Demo-User"] = "arbitrary-platform-user"
        assert (await client.get("/demo/workspace")).status_code == 401
        client.headers["X-Demo-User"] = "demo-recruiter"
        assert (
            await client.post(
                "/api/v1/profiles", json={"content": "Recruiter cannot create a candidate profile."}
            )
        ).status_code == 403
        chat = (await client.post("/api/v1/conversations", json={"language": "en"})).json()
        turn = await client.post(
            f"/api/v1/conversations/{chat['id']}/turns",
            headers={"Idempotency-Key": "demo-matching-one"},
            json={"expected_revision": 0, "message": "Find Python candidates"},
        )
        assert turn.status_code == 200, turn.text
        assert turn.json()["response"]["status"] == "matching_unavailable"


@pytest.mark.parametrize(
    "page",
    [
        "Simulation",
        "Quick preview",
        "Profile workspace",
        "CV import",
        "Applications",
        "Conversation",
        "Reference search",
        "Operations",
    ],
)
def test_streamlit_pages_render_without_credentials(monkeypatch, page):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    def request(method, url, **kwargs):
        if url.endswith("/demo/status"):
            data = {"demo": True, "ai_mode": "simulated"}
        elif url.endswith("/demo/workspace"):
            data = {"profiles": [], "drafts": [], "cv_imports": [], "conversations": []}
        elif url.endswith("/operations/status"):
            data = {"queues": {}, "providers": []}
        else:
            data = {"status": "ready"}
        return httpx.Response(200, json=data)

    monkeypatch.setattr(httpx, "request", request)
    app = AppTest.from_file(
        Path(__file__).resolve().parents[1] / "streamlit_app.py", default_timeout=15
    ).run()
    app.sidebar.radio[0].set_value(page).run()
    assert not app.exception


def test_console_selects_new_facade_pack_and_preserves_manual_selection(monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    drafts = [{"id": "old-draft", "version": 1, "status": "queued"}]
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        if url.endswith("/demo/status"):
            data = {"demo": True, "ai_mode": "simulated"}
        elif url.endswith("/demo/workspace"):
            data = {
                "profiles": [{"id": "sample-profile", "version": 1, "validated_version": 1}],
                "drafts": drafts,
                "cv_imports": [],
                "conversations": [],
            }
        elif url.endswith("/agent/generate-pack"):
            drafts.insert(0, {"id": "new-pack", "version": 1, "status": "queued"})
            data = {"draft_id": "new-pack", "task_id": "pack-task"}
        elif "/tasks/" in url:
            data = {"draft_id": "new-pack", "state": "pending"}
        else:
            data = next(row for row in drafts if url.endswith("/" + row["id"]))
        return httpx.Response(200, json=data)

    monkeypatch.setattr(httpx, "request", request)
    app = AppTest.from_file(
        Path(__file__).resolve().parents[1] / "streamlit_app.py", default_timeout=15
    ).run()
    app.sidebar.radio[0].set_value("Applications").run()
    next(button for button in app.button if button.label == "Queue synthetic pack").click().run()
    assert not app.exception
    selected = next(box for box in app.selectbox if box.label == "Drafts")
    assert selected.value == "new-pack"
    selected.set_value("old-draft").run()
    assert next(box for box in app.selectbox if box.label == "Drafts").value == "old-draft"
    payload = next(body for method, url, body in calls if url.endswith("/agent/generate-pack"))
    assert payload["offer_id"] == "fixture-python-pfe" and payload["offer_version"] == 1
    assert "job_description" not in payload
