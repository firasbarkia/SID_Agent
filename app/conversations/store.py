import hashlib
from datetime import timedelta
from uuid import uuid4

from pymongo import ReturnDocument

from app.db.connection import server_time, transaction
from app.db.store import Store, ownership, require_document
from app.domain import DomainError

MAX_TURNS = 50


def conversation_view(head):
    return {
        "id": head["_id"],
        **{
            key: head[key]
            for key in (
                "role",
                "language",
                "revision",
                "keywords",
                "profile_id",
                "profile_version",
                "created_at",
                "updated_at",
            )
        },
    }


def turn_view(turn):
    return {key: turn[key] for key in ("revision", "message", "response", "created_at")}


class ConversationStore:
    def __init__(self, db):
        self.db = db

    async def get(self, principal, conversation_id, session=None):
        return await require_document(
            self.db.sid_conversations,
            {
                "_id": conversation_id,
                **ownership(principal),
                "role": principal.role,
            },
            session,
        )

    async def profile_context(self, principal, head):
        if not head["profile_id"]:
            return None
        profile = await Store(self.db).profile(principal, head["profile_id"])
        if (
            profile["version"] != head["profile_version"]
            or profile["validated_version"] != head["profile_version"]
        ):
            raise DomainError(409, "profile_version_not_validated")
        return {"id": profile["_id"], "version": profile["version"], "content": profile["content"]}

    async def create(self, principal, payload):
        if principal.role not in {"CANDIDATE", "RECRUITER"}:
            raise DomainError(403, "conversation_role_required")
        if principal.role != "CANDIDATE" and payload.profile_id:
            raise DomainError(422, "candidate_profile_not_applicable")
        now = await server_time(self.db)
        head = {
            "_id": str(uuid4()),
            **ownership(principal),
            **payload.model_dump(),
            "role": principal.role,
            "revision": 0,
            "keywords": [],
            "created_at": now,
            "updated_at": now,
        }

        async def write(session):
            if head["profile_id"]:
                await Store(self.db)._guard_profile(
                    principal, head["profile_id"], head["profile_version"], session
                )
            await self.db.sid_conversations.insert_one(head, session=session)
            return head

        return await transaction(self.db, write)

    async def turns(self, principal, conversation_id, *, limit=MAX_TURNS):
        await self.get(principal, conversation_id)
        turns = (
            await self.db.sid_conversation_turns.find(
                {
                    "conversation_id": conversation_id,
                    **ownership(principal),
                }
            )
            .sort("revision", -1)
            .limit(limit)
            .to_list()
        )
        return list(reversed(turns))

    async def begin(self, principal, conversation_id, payload, key):
        head = await self.get(principal, conversation_id)
        request_hash = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
        old = await self.db.sid_conversation_turns.find_one(
            {
                "conversation_id": conversation_id,
                **ownership(principal),
                "idempotency_key": key,
            }
        )
        if old:
            if old["request_hash"] != request_hash:
                raise DomainError(409, "idempotency_key_reused")
            return head, None, old
        if head["revision"] != payload.expected_revision:
            raise DomainError(409, "stale_conversation_revision")
        if head["revision"] >= MAX_TURNS:
            raise DomainError(409, "conversation_turn_limit")
        now = await server_time(self.db)
        lease = {
            "token": str(uuid4()),
            "until": now + timedelta(seconds=120),
            "request_hash": request_hash,
            "key": key,
        }
        claimed = await self.db.sid_conversations.find_one_and_update(
            {
                "_id": conversation_id,
                **ownership(principal),
                "role": principal.role,
                "revision": payload.expected_revision,
                "$or": [{"pending": {"$exists": False}}, {"pending.until": {"$lte": now}}],
            },
            {"$set": {"pending": lease}},
            return_document=ReturnDocument.AFTER,
        )
        if claimed is None:
            raise DomainError(409, "conversation_busy_or_changed")
        return claimed, lease, None

    async def release(self, conversation_id, lease):
        await self.db.sid_conversations.update_one(
            {
                "_id": conversation_id,
                "pending.token": lease["token"],
            },
            {"$unset": {"pending": ""}},
        )

    async def complete(self, principal, head, lease, payload, response, keywords):
        now = await server_time(self.db)
        turn = {
            "_id": str(uuid4()),
            **ownership(principal),
            "conversation_id": head["_id"],
            "revision": head["revision"] + 1,
            "idempotency_key": lease["key"],
            "request_hash": lease["request_hash"],
            "message": payload.message,
            "response": response,
            "created_at": now,
        }

        async def write(session):
            if head["profile_id"]:
                await Store(self.db)._guard_profile(
                    principal, head["profile_id"], head["profile_version"], session
                )
            updated = await self.db.sid_conversations.update_one(
                {
                    "_id": head["_id"],
                    **ownership(principal),
                    "role": principal.role,
                    "revision": head["revision"],
                    "pending.token": lease["token"],
                    "pending.until": {"$gt": now},
                },
                {
                    "$inc": {"revision": 1},
                    "$set": {"keywords": keywords, "updated_at": now},
                    "$unset": {"pending": ""},
                },
                session=session,
            )
            if not updated.matched_count:
                raise DomainError(409, "conversation_lease_lost")
            await self.db.sid_conversation_turns.insert_one(turn, session=session)
            return turn

        return await transaction(self.db, write)
