"""Bound body buffering before FastAPI parses JSON or resolves dependencies."""

import asyncio

from starlette.responses import JSONResponse

from app.cv.pdf import MAX_BYTES


class BodyLimits:
    def __init__(self, app, settings):
        self.app = app
        self.settings = settings
        self.active = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["method"] == "GET" and scope["path"] in {"/health", "/health/ready"}:
            # These handlers never consume a body. Let supervisors probe a busy API
            # without reserving buffering capacity or waiting for request content.
            return await self.app(scope, receive, send)
        # Only the raw PDF upload gets the larger allowance, regardless of content type.
        pdf = scope["method"] == "POST" and scope["path"] in {
            "/api/v1/cv-imports",
            "/agent/parse-cv",
        }
        limit = MAX_BYTES if pdf else self.settings.http_json_max_bytes

        async def reject(status, code):
            await JSONResponse({"detail": {"code": code}}, status_code=status)(scope, receive, send)

        lengths = [value for name, value in scope["headers"] if name.lower() == b"content-length"]
        if lengths:
            try:
                if len(lengths) != 1 or not lengths[0].isdigit():
                    raise ValueError
                length = int(lengths[0])
            except ValueError:
                return await reject(400, "invalid_content_length")
            if length > limit:
                return await reject(413, "cv_file_too_large" if pdf else "request_body_too_large")
        if self.active >= self.settings.http_max_concurrent_bodies:
            return await reject(503, "request_body_capacity_exceeded")
        # No await between checking and reserving: atomic within this ASGI event loop.
        self.active += 1
        try:
            data = bytearray()
            try:
                async with asyncio.timeout(self.settings.http_body_timeout_seconds):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        chunk = message.get("body", b"")
                        if len(data) + len(chunk) > limit:
                            return await reject(
                                413, "cv_file_too_large" if pdf else "request_body_too_large"
                            )
                        data.extend(chunk)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                return await reject(408, "request_body_timeout")
            delivered = False

            async def bounded_receive():
                nonlocal delivered
                if delivered:
                    return await receive()
                delivered = True
                return {"type": "http.request", "body": bytes(data), "more_body": False}

            await self.app(scope, bounded_receive, send)
        finally:
            self.active -= 1
