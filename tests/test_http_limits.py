import asyncio

import httpx
import pytest

from app.http_limits import BodyLimits
from app.main import create_app


def scope(headers=(), path="/api/v1/application-drafts/preview"):
    return {"type": "http", "method": "POST", "path": path, "headers": list(headers)}


class Messages(list):
    async def send(self, message):
        self.append(message)


@pytest.mark.parametrize("declared", [True, False])
async def test_oversized_unauthenticated_json_never_reaches_fastapi(settings, declared):
    application = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:

        async def body():
            yield b"x" * settings.http_json_max_bytes
            yield b"x"

        response = await client.post(
            "/api/v1/application-drafts/preview",
            content=b"x" * (settings.http_json_max_bytes + 1) if declared else body(),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "request_body_too_large"


async def test_declared_length_rejected_without_read_and_chunked_stops_early(settings):
    calls, sent = [], Messages()

    async def forbidden(*args):
        calls.append(True)
        raise AssertionError("Must not read or parse")

    middleware = BodyLimits(forbidden, settings)
    await middleware(scope([(b"content-length", b"999999999")]), forbidden, sent.send)
    assert sent[0]["status"] == 413 and not calls
    chunks = iter([b"x" * settings.http_json_max_bytes, b"x", b"unread"])

    async def receive():
        calls.append(True)
        return {"type": "http.request", "body": next(chunks), "more_body": True}

    sent.clear()
    await middleware(scope(), receive, sent.send)
    assert sent[0]["status"] == 413 and len(calls) == 2
    assert next(chunks) == b"unread"
    assert middleware.active == 0


async def test_body_deadline_and_concurrent_admission_release(settings):
    settings.http_body_timeout_seconds = 0.03
    settings.http_max_concurrent_bodies = 1
    started = asyncio.Event()
    blocked, overflow, healthy = Messages(), Messages(), Messages()

    async def slow():
        started.set()
        await asyncio.Event().wait()

    async def app(scope, receive, send):
        assert (await receive())["body"] == b"ok"
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def receive():
        return {"type": "http.request", "body": b"ok", "more_body": False}

    middleware = BodyLimits(app, settings)
    task = asyncio.create_task(middleware(scope(), slow, blocked.send))
    await started.wait()
    await middleware(scope(), receive, overflow.send)
    assert overflow[0]["status"] == 503
    await task
    assert blocked[0]["status"] == 408 and middleware.active == 0
    await middleware(scope(), receive, healthy.send)
    assert healthy[0]["status"] == 200 and middleware.active == 0


async def test_pdf_has_separate_limit_and_disguised_json_does_not(settings):
    called, sent = [], Messages()

    async def app(scope, receive, send):
        called.append((await receive())["body"])

    async def receive():
        return {"type": "http.request", "body": b"x" * (settings.http_json_max_bytes + 1)}

    middleware = BodyLimits(app, settings)
    await middleware(scope(path="/api/v1/cv-imports"), receive, sent.send)
    assert len(called) == 1 and not sent
    await middleware(scope([(b"content-type", b"application/pdf")]), receive, sent.send)
    assert sent[0]["status"] == 413 and len(called) == 1


async def test_disconnect_and_application_errors_release_capacity(settings):
    async def app(*args):
        raise RuntimeError("programming error")

    async def disconnect():
        return {"type": "http.disconnect"}

    async def receive():
        return {"type": "http.request", "body": b""}

    middleware = BodyLimits(app, settings)
    await middleware(scope(), disconnect, Messages().send)
    assert middleware.active == 0
    with pytest.raises(RuntimeError, match="programming error"):
        await middleware(scope(), receive, Messages().send)
    assert middleware.active == 0


async def test_health_probe_works_when_body_capacity_is_full(settings):
    application = create_app(settings)
    middleware = BodyLimits(application, settings)
    middleware.active = settings.http_max_concurrent_bodies
    sent = Messages()

    async def forbidden():
        raise AssertionError("Health must not buffer request content")

    health_scope = {
        **scope(),
        "method": "GET",
        "path": "/health",
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("test", 1),
        "root_path": "",
        "http_version": "1.1",
    }
    await middleware(health_scope, forbidden, sent.send)
    assert sent[0]["status"] == 200
    assert middleware.active == settings.http_max_concurrent_bodies
