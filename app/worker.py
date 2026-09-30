"""Run a separately scalable generation worker: uv run python -m app.worker."""

import asyncio
import json
import logging
from contextlib import suppress

import httpx
from pymongo.errors import PyMongoError

from app.ai.errors import (
    ProviderRefused,
    ProviderRequestRejected,
    ProvidersUnavailable,
    ServiceBusy,
)
from app.ai.gateway import AIGateway
from app.ai.mongo_circuit import shared_breakers
from app.ai.providers import GeminiProvider, GroqProvider
from app.api.drafts import SYSTEM_PROMPT
from app.config import Settings
from app.db.connection import ensure_indexes, mongo_connection, server_time, transaction
from app.db.queue import LeaseLost, MongoQueue
from app.db.store import Store, emit, ownership
from app.domain import DomainError
from app.identity import Principal
from app.platform import PlatformReader

logger = logging.getLogger(__name__)


class DraftWorker:
    def __init__(self, db, gateway, settings):
        self.db = db
        self.gateway = gateway
        self.settings = settings
        self.store = Store(db, settings.worker_max_attempts)
        self.queue = MongoQueue(db, lease_seconds=settings.worker_lease_seconds)
        self.platform = PlatformReader(db, settings)

    async def _heartbeat(self, task):
        while True:
            await asyncio.sleep(self.settings.worker_lease_seconds / 3)
            await self.queue.heartbeat(task)

    async def run_once(self):
        task = await self.queue.claim()
        if task is None:
            return False
        work = asyncio.create_task(self._process(task))
        heartbeat = asyncio.create_task(self._heartbeat(task))
        try:
            done, _ = await asyncio.wait({work, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                await work
            else:
                await heartbeat  # A failed lease stops generation and prevents commit.
        except LeaseLost:
            logger.warning("Worker lost its task lease")
        finally:
            for running in (work, heartbeat):
                running.cancel()
            await asyncio.gather(work, heartbeat, return_exceptions=True)
        return True

    async def _process(self, task):
        if task["attempts"] > task["max_attempts"]:
            await self._fail(task, "attempts_exhausted", permanent=True)
            return
        if task["type"] != "draft.generate":
            await self._fail(task, "unknown_task_type", permanent=True)
            return
        principal = Principal(user_id=task["owner_id"], role="CANDIDATE")
        if task["scope_id"] != principal.scope_id:
            await self._fail(task, "invalid_task_scope", permanent=True)
            return
        try:
            current_user = await self.platform.principal(task["owner_id"])
            if current_user.role != "CANDIDATE":
                raise DomainError(403, "candidate_access_required")
            head = await self.store.draft(principal, task["draft_id"])
            profile = await self.store.profile(principal, head["profile_id"])
            if (
                profile["version"] != head["profile_version"]
                or profile["validated_version"] != head["profile_version"]
            ):
                raise DomainError(409, "profile_version_not_validated")
            if head["version"] != 0 or head["status"] != "queued":
                raise DomainError(409, "draft_not_queued")
            snapshot = await self.db.sid_profile_versions.find_one(
                {
                    "profile_id": head["profile_id"],
                    "version": head["profile_version"],
                    **ownership(principal),
                }
            )
            if snapshot is None:
                raise DomainError(409, "profile_snapshot_missing")
            generation = await self.gateway.generate(
                SYSTEM_PROMPT,
                json.dumps(
                    {
                        "candidate_profile": snapshot["content"],
                        "job_description": head["job_description"],
                        "company_context": head["company_context"],
                        "language": head["language"],
                    },
                    ensure_ascii=False,
                ),
            )
            current_user = await self.platform.principal(task["owner_id"])
            if current_user.role != "CANDIDATE":
                raise DomainError(403, "candidate_access_required")
            await self._complete(task, principal, head, generation)
        except (ProviderRefused, ProviderRequestRejected) as exc:
            code = (
                "generation_blocked"
                if isinstance(exc, ProviderRefused)
                else "provider_request_rejected"
            )
            await self._fail(task, code, permanent=True)
        except DomainError as exc:
            await self._fail(task, exc.code, permanent=True)
        except (ProvidersUnavailable, ServiceBusy) as exc:
            await self._fail(task, "ai_unavailable", retry_after=getattr(exc, "retry_after", 1))

    async def _complete(self, task, principal, head, generation):
        now = await server_time(self.db)

        async def write(session):
            await self.store._guard_profile(
                principal, head["profile_id"], head["profile_version"], session
            )
            await self.queue.finish(task, session=session)
            updated = {
                **head,
                "version": 1,
                "status": "draft",
                "updated_at": now,
                "cover_letter": generation.text,
                "provider": generation.provider,
                "model": generation.model,
                "fallback_used": generation.fallback_used,
            }
            result = await self.db.sid_drafts.replace_one(
                {"_id": head["_id"], **ownership(principal), "version": 0, "status": "queued"},
                updated,
                session=session,
            )
            if not result.matched_count:
                raise DomainError(409, "draft_not_queued")
            await self.store._draft_version(updated, session)
            await emit(
                self.db,
                head["_id"],
                "generated:1",
                "draft.generated",
                principal,
                {"draft_id": head["_id"], "version": 1},
                now,
                session,
            )

        await transaction(self.db, write)

    async def _fail(self, task, code, permanent=False, retry_after=1):
        final = permanent or task["attempts"] >= task["max_attempts"]

        async def write(session):
            await self.queue.finish(
                task,
                state="failed" if final else "pending",
                error_code=code,
                retry_after=max(1, min(86400, retry_after)),
                session=session,
            )
            if final:
                await self.db.sid_drafts.update_one(
                    {"_id": task["draft_id"], "version": 0, "status": "queued"},
                    {"$set": {"status": "failed", "error_code": code}},
                    session=session,
                )

        await transaction(self.db, write)


async def main():
    settings = Settings()
    async with mongo_connection(settings) as db:
        if db is None:
            raise RuntimeError("Configure MONGODB_URI before starting a worker")
        await ensure_indexes(db)
        async with httpx.AsyncClient(
            timeout=settings.ai_provider_timeout_seconds, follow_redirects=False
        ) as client:
            gateway = AIGateway(
                [
                    GroqProvider(
                        client, settings.groq_api_key.get_secret_value(), settings.groq_model
                    ),
                    GeminiProvider(
                        client, settings.gemini_api_key.get_secret_value(), settings.gemini_model
                    ),
                ],
                settings,
                breakers=await shared_breakers(db, settings),
            )
            if not any(
                provider["configured"] and provider["quota_configured"]
                for provider in await gateway.status()
            ):
                raise RuntimeError(
                    "Configure provider keys and shared quota limits before running workers"
                )
            worker = DraftWorker(db, gateway, settings)
            while True:
                try:
                    if not await worker.run_once():
                        await asyncio.sleep(settings.worker_poll_seconds)
                except PyMongoError:
                    logger.warning("Worker database unavailable; leased work will be retried")
                    await asyncio.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        asyncio.run(main())
