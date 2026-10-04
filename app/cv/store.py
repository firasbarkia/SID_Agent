import hashlib
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.cv.models import (
    conflicts,
    evaluate,
    profile_content,
    review_warnings,
    source_warnings,
)
from app.db.connection import server_time, transaction
from app.db.store import Store, emit, ownership, require_document
from app.domain import DomainError, ProfileCreate


def view(head):
    allowed = {
        "status",
        "version",
        "language",
        "sha256",
        "byte_count",
        "facts",
        "pages",
        "warnings",
        "error_code",
        "profile_id",
        "provider",
        "model",
        "fallback_used",
        "created_at",
        "updated_at",
    }
    return {
        "id": head["_id"],
        **{key: value for key, value in head.items() if key in allowed},
        "requires_human_approval": head["status"] != "accepted",
        "evaluation": evaluate(head.get("facts", []))
        if head["status"] in {"review", "accepted"}
        else None,
    }


class CVStore:
    def __init__(self, db, max_attempts=3):
        self.db = db
        self.max_attempts = max_attempts

    async def get(self, principal, import_id, session=None):
        head = await require_document(
            self.db.sid_cv_imports, {"_id": import_id, **ownership(principal)}, session
        )
        if head["status"] == "deleted":
            raise DomainError(410, "cv_import_deleted")
        return head

    async def upload(self, principal, data, key, language):
        now = await server_time(self.db)
        digest = hashlib.sha256(data).hexdigest()
        query = {**ownership(principal), "idempotency_key": key}

        def same(head):
            if head["sha256"] != digest or head["language"] != language:
                raise DomainError(409, "idempotency_key_reused")
            if head["status"] == "deleted":
                raise DomainError(410, "cv_import_deleted")
            return head

        head = {
            "_id": str(uuid4()),
            **query,
            "status": "queued",
            "version": 0,
            "language": language,
            "sha256": digest,
            "byte_count": len(data),
            "created_at": now,
            "updated_at": now,
        }

        async def write(session):
            old = await self.db.sid_cv_imports.find_one(query, session=session)
            if old is not None:
                return same(old)
            await self.db.sid_cv_imports.insert_one(head, session=session)
            await self.db.sid_cv_files.insert_one(
                {"_id": head["_id"], **ownership(principal), "data": data}, session=session
            )
            await self.db.sid_cv_tasks.insert_one(
                {
                    "_id": str(uuid4()),
                    **ownership(principal),
                    "import_id": head["_id"],
                    "type": "cv.extract",
                    "state": "pending",
                    "attempts": 0,
                    "max_attempts": self.max_attempts,
                    "available_at": now,
                    "created_at": now,
                },
                session=session,
            )
            return head

        try:
            return await transaction(self.db, write)
        except DuplicateKeyError:
            return same(await require_document(self.db.sid_cv_imports, query))

    async def version(self, head, session):
        await self.db.sid_cv_versions.insert_one(
            {
                "_id": f"{head['_id']}:{head['version']}",
                "import_id": head["_id"],
                "owner_id": head["owner_id"],
                "scope_id": head["scope_id"],
                "version": head["version"],
                "facts": head["facts"],
                "warnings": head["warnings"],
                "created_at": head["updated_at"],
            },
            session=session,
        )

    async def edit(self, principal, import_id, payload):
        now = await server_time(self.db)

        async def write(session):
            old = await self.get(principal, import_id, session)
            if old["status"] != "review" or old["version"] != payload.expected_version:
                raise DomainError(409, "cv_version_not_editable")
            supported = {(f["field"], f["value"]): f for f in old["facts"]}
            facts = [
                supported.get(
                    (f.field, f.value),
                    {
                        **f.model_dump(),
                        "origin": "candidate_asserted",
                        "evidence": None,
                    },
                )
                for f in payload.facts
            ]
            head = await self.db.sid_cv_imports.find_one_and_update(
                {
                    "_id": import_id,
                    **ownership(principal),
                    "status": "review",
                    "version": payload.expected_version,
                },
                {
                    "$inc": {"version": 1},
                    "$set": {
                        "facts": facts,
                        "warnings": review_warnings(facts) + source_warnings(old["pages"]),
                        "updated_at": now,
                    },
                },
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if head is None:
                raise DomainError(409, "cv_version_not_editable")
            await self.version(head, session)
            return head

        return await transaction(self.db, write)

    async def accept(self, principal, import_id, expected_version):
        now = await server_time(self.db)

        async def write(session):
            head = await self.get(principal, import_id, session)
            if head["version"] != expected_version:
                raise DomainError(409, "stale_cv_version")
            if head["status"] == "accepted":
                return head
            if head["status"] != "review":
                raise DomainError(409, "cv_not_ready_for_review")
            if conflicts(head["facts"]):
                raise DomainError(409, "cv_conflicts_require_correction")
            profile = await Store(self.db).create_profile(
                principal,
                ProfileCreate(content=profile_content(head["facts"])),
                structured_data={
                    "schema_version": 1,
                    "cv_import_id": import_id,
                    "cv_version": expected_version,
                    "facts": head["facts"],
                },
                validated=True,
                session=session,
            )
            updated = await self.db.sid_cv_imports.find_one_and_update(
                {
                    "_id": import_id,
                    **ownership(principal),
                    "version": expected_version,
                    "status": "review",
                },
                {"$set": {"status": "accepted", "profile_id": profile["_id"], "updated_at": now}},
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if updated is None:
                raise DomainError(409, "stale_cv_version")
            await emit(
                self.db,
                profile["_id"],
                "accepted:1",
                "profile.accepted",
                principal,
                {"profile_id": profile["_id"], "version": 1},
                now,
                session,
            )
            return updated

        return await transaction(self.db, write)

    async def delete(self, principal, import_id):
        async def write(session):
            await self.get(principal, import_id, session)
            await self.db.sid_cv_files.delete_one({"_id": import_id}, session=session)
            await self.db.sid_cv_versions.delete_many({"import_id": import_id}, session=session)
            await self.db.sid_cv_tasks.delete_many({"import_id": import_id}, session=session)
            # Keep only a tombstone for idempotency. Accepted profiles have their own lifecycle.
            head = await self.db.sid_cv_imports.find_one({"_id": import_id}, session=session)
            tombstone = {
                k: head[k]
                for k in ("_id", "owner_id", "scope_id", "idempotency_key", "sha256", "language")
            }
            await self.db.sid_cv_imports.replace_one(
                {"_id": import_id}, {**tombstone, "status": "deleted"}, session=session
            )

        await transaction(self.db, write)
