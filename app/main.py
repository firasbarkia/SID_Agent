import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError

from app.ai.gateway import AIGateway
from app.ai.mongo_circuit import shared_breakers
from app.ai.providers import GeminiProvider, GroqProvider
from app.api.conversations import router as conversations_router
from app.api.cv import router as cv_router
from app.api.drafts import router
from app.api.knowledge import router as knowledge_router
from app.api.operations import router as operations_router
from app.api.persistence import router as persistence_router
from app.config import Settings
from app.db.connection import ensure_indexes, mongo_connection
from app.db.store import Store
from app.domain import DomainError
from app.identity import UserIdVerifier
from app.knowledge.embedding import LocalEmbedder
from app.knowledge.qdrant import QdrantIndex, qdrant_client
from app.knowledge.search import KnowledgeSearch
from app.platform import PlatformReader


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    verify_user_id: UserIdVerifier | None = None,
    knowledge_embedder=None,
) -> FastAPI:
    config = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        async with (
            mongo_connection(config) as db,
            httpx.AsyncClient(
                timeout=config.ai_provider_timeout_seconds,
                follow_redirects=False,
                transport=transport,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            ) as client,
        ):
            application.state.knowledge_search = None
            application.state.store = (
                Store(db, config.worker_max_attempts) if db is not None else None
            )
            application.state.platform = PlatformReader(db, config) if db is not None else None
            breakers = None
            if db is not None:
                await ensure_indexes(db)
                breakers = await shared_breakers(db, config)
            application.state.ai_gateway = AIGateway(
                [
                    GroqProvider(client, config.groq_api_key.get_secret_value(), config.groq_model),
                    GeminiProvider(
                        client, config.gemini_api_key.get_secret_value(), config.gemini_model
                    ),
                ],
                config,
                breakers=breakers,
            )
            if db is not None and config.qdrant_url:
                embedder = knowledge_embedder or await asyncio.to_thread(
                    LocalEmbedder, config.embedding_model_directory, config.embedding_threads
                )
                async with qdrant_client(config) as search_client:
                    index = QdrantIndex(search_client, embedder.dimensions, embedder.fingerprint)
                    application.state.knowledge_search = KnowledgeSearch(
                        db, index, embedder, config.knowledge_max_concurrent_queries
                    )
                    try:
                        yield
                    finally:
                        await application.state.knowledge_search.close()
            else:
                yield

    application = FastAPI(title="SID Agent API", version="0.6.0", lifespan=lifespan)
    application.state.settings = config

    async def resolve_identity(request):
        if application.state.platform is None:
            raise HTTPException(503, detail={"code": "mongodb_not_configured"})
        user_id = await verify_user_id(request)
        if not isinstance(user_id, str) or not user_id.strip():
            raise HTTPException(401, detail={"code": "invalid_platform_identity"})
        return await application.state.platform.principal(user_id)

    application.state.identity_resolver = resolve_identity if verify_user_id is not None else None
    application.include_router(router)
    application.include_router(persistence_router)
    application.include_router(knowledge_router)
    application.include_router(cv_router)
    application.include_router(conversations_router)
    application.include_router(operations_router)

    @application.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({"detail": {"code": exc.code}}, status_code=exc.status)

    @application.exception_handler(PyMongoError)
    async def database_error(request, exc):
        return JSONResponse(
            {"detail": {"code": "database_unavailable"}},
            status_code=503,
            headers={"Retry-After": "5"},
        )

    @application.get("/")
    def root() -> dict[str, str]:
        return {"message": "Welcome to SID Agent API"}

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready")
    async def ready() -> JSONResponse:
        # Configuration readiness only: no paid or quota-consuming health probes.
        configured = bool(
            config.sid_service_api_key.get_secret_value()
            and (config.groq_api_key.get_secret_value() or config.gemini_api_key.get_secret_value())
        )
        if config.mongodb_uri.get_secret_value():
            store = application.state.store
            try:
                await store.db.command("ping")
                database_available = True
            except PyMongoError:
                database_available = False
            limits_ready = any(
                provider["configured"] and provider["quota_configured"]
                for provider in await application.state.ai_gateway.status()
            )
            checks = {
                "mongodb": database_available,
                "identity": verify_user_id is not None,
                "provider_limits": limits_ready,
            }
            ready = configured and all(checks.values())
            return JSONResponse(
                {"status": "ready" if ready else "not_ready", "checks": checks},
                status_code=200 if ready else 503,
            )
        return JSONResponse(
            {"status": "ready" if configured else "not_configured", "check": "configuration"},
            status_code=200 if configured else 503,
        )

    return application


app = create_app()
