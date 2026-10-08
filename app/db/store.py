import hashlib
import json
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.db.connection import server_time, transaction
from app.domain import DomainError


def ownership(principal):
    return {"owner_id": principal.user_id, "scope_id": principal.scope_id}


def public(document):
    hidden = {
        "_id",
        "approval_guard",
        "lease_token",
        "lease_until",
        "request_hash",
        "idempotency_key",
    }
    return {"id": document["_id"], **{k: v for k, v in document.items() if k not in hidden}}


async def require_document(collection, query, session=None):
    document = await collection.find_one(query, session=session)
    if document is None:
        raise DomainError(404, "record_not_found")
    return document


async def emit(db, aggregate_id, event_key, event_type, principal, payload, now, session):
    await db.sid_outbox.insert_one(
        {
            "_id": str(uuid4()),
            **ownership(principal),
            "aggregate_id": aggregate_id,
            "event_key": event_key,
            "type": event_type,
            "payload": payload,
            "state": "pending",
            "attempts": 0,
            "available_at": now,
            "created_at": now,
        },
        session=session,
    )


class Store:
    def __init__(self, db, max_attempts=3):
        self.db = db
        self.max_attempts = max_attempts

    async def profile(self, principal, profile_id):
        return await require_document(
            self.db.sid_profiles, {"_id": profile_id, **ownership(principal)}
        )

    async def draft(self, principal, draft_id):
        return await require_document(self.db.sid_drafts, {"_id": draft_id, **ownership(principal)})

    async def create_profile(
        self, principal, payload, *, structured_data=None, session=None, validated=False
    ):
        now = await server_time(self.db)
        profile_id = str(uuid4())
        head = {
            "_id": profile_id,
            **ownership(principal),
            "version": 1,
            **payload.model_dump(),
            "structured_data": structured_data,
            "validated_version": 1 if validated else None,
            "approval_guard": 0,
            "created_at": now,
            "updated_at": now,
        }

        async def write(session):
            await self.db.sid_profiles.insert_one(head, session=session)
            await self._profile_version(head, session)
            await emit(
                self.db,
                profile_id,
                "created:1",
                "profile.created",
                principal,
                {"profile_id": profile_id, "version": 1},
                now,
                session,
            )
            return head

        return await write(session) if session is not None else await transaction(self.db, write)

    async def _profile_version(self, head, session):
        await self.db.sid_profile_versions.insert_one(
            {
                "_id": f"{head['_id']}:{head['version']}",
                "profile_id": head["_id"],
                **ownership_from(head),
                "version": head["version"],
                "content": head["content"],
                "structured_data": head.get("structured_data"),
                "external_profile_id": head.get("external_profile_id"),
                "created_at": head["updated_at"],
            },
            session=session,
        )

    async def edit_profile(self, principal, profile_id, payload):
        now = await server_time(self.db)

        async def write(session):
            await require_document(
                self.db.sid_profiles, {"_id": profile_id, **ownership(principal)}, session
            )
            head = await self.db.sid_profiles.find_one_and_update(
                {"_id": profile_id, **ownership(principal), "version": payload.expected_version},
                {
                    "$inc": {"version": 1},
                    "$set": {
                        "content": payload.content,
                        "structured_data": None,
                        "validated_version": None,
                        "updated_at": now,
                    },
                },
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if head is None:
                raise DomainError(409, "stale_profile_version")
            await self._profile_version(head, session)
            await self.db.sid_drafts.update_many(
                {**ownership(principal), "profile_id": profile_id, "version": {"$gt": 0}},
                {
                    "$set": {
                        "status": "draft",
                        "requires_human_approval": True,
                        "approval_id": None,
                        "updated_at": now,
                    }
                },
                session=session,
            )
            await self.db.sid_approvals.update_many(
                {**ownership(principal), "profile_id": profile_id, "active": True},
                {"$set": {"active": False, "invalidated_at": now}},
                session=session,
            )
            await emit(
                self.db,
                profile_id,
                f"edited:{head['version']}",
                "profile.edited",
                principal,
                {"profile_id": profile_id, "version": head["version"]},
                now,
                session,
            )
            return head

        return await transaction(self.db, write)

    async def accept_profile(self, principal, profile_id, expected_version):
        now = await server_time(self.db)

        async def write(session):
            head = await require_document(
                self.db.sid_profiles, {"_id": profile_id, **ownership(principal)}, session
            )
            if head["version"] != expected_version:
                raise DomainError(409, "stale_profile_version")
            if head["validated_version"] == expected_version:
                return head
            await self.db.sid_profiles.update_one(
                {"_id": profile_id, "version": expected_version},
                {"$set": {"validated_version": expected_version, "updated_at": now}},
                session=session,
            )
            await emit(
                self.db,
                profile_id,
                f"accepted:{expected_version}",
                "profile.accepted",
                principal,
                {"profile_id": profile_id, "version": expected_version},
                now,
                session,
            )
            return {**head, "validated_version": expected_version, "updated_at": now}

        return await transaction(self.db, write)

    async def _guard_profile(self, principal, profile_id, version, session):
        profile = await self.db.sid_profiles.find_one_and_update(
            {
                "_id": profile_id,
                **ownership(principal),
                "version": version,
                "validated_version": version,
            },
            {"$inc": {"approval_guard": 1}},
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if profile is None:
            # Ensure wrong ownership does not reveal a record's validation state.
            await require_document(
                self.db.sid_profiles, {"_id": profile_id, **ownership(principal)}, session
            )
            raise DomainError(409, "profile_version_not_validated")
        return profile

    async def request_draft(self, principal, payload, idempotency_key, *, dossier=False):
        now = await server_time(self.db)
        draft_id, task_id = str(uuid4()), str(uuid4())
        request_bytes = payload.model_dump_json().encode()
        # Preserve existing letter-only idempotency hashes.
        request_hash = hashlib.sha256(
            request_bytes + (b":dossier:v1" if dossier else b"")
        ).hexdigest()
        task_query = {**ownership(principal), "idempotency_key": idempotency_key}

        def existing(task):
            if task["request_hash"] != request_hash:
                raise DomainError(409, "idempotency_key_reused")
            return {"draft_id": task["draft_id"], "task_id": task["_id"], "status": task["state"]}

        async def write(session):
            task = await self.db.sid_tasks.find_one(task_query, session=session)
            if task is not None:
                return existing(task)
            await self._guard_profile(
                principal, payload.profile_id, payload.profile_version, session
            )
            await self.db.sid_drafts.insert_one(
                {
                    "_id": draft_id,
                    **ownership(principal),
                    **payload.model_dump(),
                    "dossier_requested": dossier,
                    "version": 0,
                    "status": "queued",
                    "requires_human_approval": True,
                    "approval_id": None,
                    "created_at": now,
                    "updated_at": now,
                },
                session=session,
            )
            await self.db.sid_tasks.insert_one(
                {
                    "_id": task_id,
                    **task_query,
                    "request_hash": request_hash,
                    "draft_id": draft_id,
                    "type": "draft.generate",
                    "state": "pending",
                    "attempts": 0,
                    "max_attempts": self.max_attempts,
                    "available_at": now,
                    "created_at": now,
                },
                session=session,
            )
            await emit(
                self.db,
                draft_id,
                "requested",
                "draft.requested",
                principal,
                {"draft_id": draft_id, "task_id": task_id},
                now,
                session,
            )
            return {"draft_id": draft_id, "task_id": task_id, "status": "pending"}

        try:
            return await transaction(self.db, write)
        except DuplicateKeyError:
            # A concurrent transaction may have won the idempotency-key race.
            return existing(await require_document(self.db.sid_tasks, task_query))

    async def _draft_version(self, head, session):
        await self.db.sid_draft_versions.insert_one(
            {
                "_id": f"{head['_id']}:{head['version']}",
                "draft_id": head["_id"],
                **ownership_from(head),
                "version": head["version"],
                "cover_letter": head["cover_letter"],
                "dossier_analysis": head.get("dossier_analysis"),
                "source_snapshot": head.get("source_snapshot"),
                "analysis_letter_version": head.get("analysis_letter_version"),
                "profile_id": head["profile_id"],
                "profile_version": head["profile_version"],
                "job_description": head["job_description"],
                "company_context": head["company_context"],
                "language": head["language"],
                "created_at": head["updated_at"],
                "provider": head.get("provider"),
                "model": head.get("model"),
            },
            session=session,
        )

    async def edit_draft(self, principal, draft_id, payload):
        now = await server_time(self.db)

        async def write(session):
            await require_document(
                self.db.sid_drafts, {"_id": draft_id, **ownership(principal)}, session
            )
            head = await self.db.sid_drafts.find_one_and_update(
                {"_id": draft_id, **ownership(principal), "version": payload.expected_version},
                {
                    "$inc": {"version": 1},
                    "$set": {
                        "cover_letter": payload.cover_letter,
                        "analysis_letter_version": None,
                        "status": "draft",
                        "requires_human_approval": True,
                        "approval_id": None,
                        "updated_at": now,
                    },
                },
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if head is None:
                raise DomainError(409, "stale_draft_version")
            await self._draft_version(head, session)
            await self.db.sid_approvals.update_many(
                {"draft_id": draft_id, **ownership(principal), "active": True},
                {"$set": {"active": False, "invalidated_at": now}},
                session=session,
            )
            await emit(
                self.db,
                draft_id,
                f"edited:{head['version']}",
                "draft.edited",
                principal,
                {"draft_id": draft_id, "version": head["version"]},
                now,
                session,
            )
            return head

        return await transaction(self.db, write)

    async def approve_draft(self, principal, draft_id, payload):
        now, approval_id = await server_time(self.db), str(uuid4())

        async def write(session):
            head = await require_document(
                self.db.sid_drafts, {"_id": draft_id, **ownership(principal)}, session
            )
            if head["version"] != payload.expected_version:
                raise DomainError(409, "stale_draft_version")
            await self._guard_profile(
                principal, head["profile_id"], head["profile_version"], session
            )
            if head.get("approval_id"):
                prior = await self.db.sid_approvals.find_one(
                    {"_id": head["approval_id"]}, session=session
                )
                if prior and prior["active"] and prior["destination_id"] == payload.destination_id:
                    return prior
            approved_content = {
                "letter": head["cover_letter"],
                "profile_version": head["profile_version"],
                "job": head["job_description"],
                "company": head["company_context"],
                "destination": payload.destination_id,
            }
            if head.get("dossier_requested"):
                approved_content.update(
                    {
                        "dossier_analysis": head.get("dossier_analysis"),
                        "source_snapshot": head.get("source_snapshot"),
                    }
                )
            digest = hashlib.sha256(
                json.dumps(
                    approved_content,
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            approval = {
                "_id": approval_id,
                **ownership(principal),
                "draft_id": draft_id,
                "draft_version": head["version"],
                "profile_id": head["profile_id"],
                "profile_version": head["profile_version"],
                "destination_id": payload.destination_id,
                "content_hash": digest,
                "active": True,
                "approved_at": now,
            }
            await self.db.sid_approvals.update_many(
                {"draft_id": draft_id, "active": True},
                {"$set": {"active": False, "invalidated_at": now}},
                session=session,
            )
            await self.db.sid_approvals.insert_one(approval, session=session)
            await self.db.sid_drafts.update_one(
                {"_id": draft_id, "version": payload.expected_version},
                {
                    "$set": {
                        "status": "approved",
                        "approval_id": approval_id,
                        "requires_human_approval": False,
                        "updated_at": now,
                    }
                },
                session=session,
            )
            await emit(
                self.db,
                draft_id,
                f"approved:{approval_id}",
                "draft.approved",
                principal,
                {"draft_id": draft_id, "approval_id": approval_id, "version": head["version"]},
                now,
                session,
            )
            return approval

        return await transaction(self.db, write)


def ownership_from(head):
    return {"owner_id": head["owner_id"], "scope_id": head["scope_id"]}
