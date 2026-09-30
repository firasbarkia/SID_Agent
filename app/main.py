from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.ai.gateway import AIGateway
from app.ai.providers import GeminiProvider, GroqProvider
from app.api.drafts import router
from app.config import Settings


def create_app(
    settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    config = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        async with httpx.AsyncClient(
            timeout=config.ai_provider_timeout_seconds,
            follow_redirects=False,
            transport=transport,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        ) as client:
            application.state.ai_gateway = AIGateway(
                [
                    GroqProvider(client, config.groq_api_key.get_secret_value(), config.groq_model),
                    GeminiProvider(
                        client, config.gemini_api_key.get_secret_value(), config.gemini_model
                    ),
                ],
                config,
            )
            yield

    application = FastAPI(title="SID Agent API", version="0.2.0", lifespan=lifespan)
    application.state.settings = config
    application.include_router(router)

    @application.get("/")
    def root() -> dict[str, str]:
        return {"message": "Welcome to SID Agent API"}

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready")
    def ready() -> JSONResponse:
        # Configuration readiness only: no paid or quota-consuming health probes.
        configured = bool(
            config.sid_service_api_key.get_secret_value()
            and (config.groq_api_key.get_secret_value() or config.gemini_api_key.get_secret_value())
        )
        return JSONResponse(
            {"status": "ready" if configured else "not_configured", "check": "configuration"},
            status_code=200 if configured else 503,
        )

    return application


app = create_app()
