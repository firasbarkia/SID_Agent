"""Small asynchronous REST adapter; all point payloads contain reference IDs only."""

import re
from uuid import NAMESPACE_URL, uuid5

import httpx

from app.knowledge.embedding import validate_vector


def point_id(generation, record_id, version, chunk):
    # Late old writes cannot overwrite a newer version's vector.
    return str(uuid5(NAMESPACE_URL, f"sid:{generation}:{record_id}:{version}:{chunk}"))


class QdrantIndex:
    def __init__(self, client: httpx.AsyncClient, dimensions, fingerprint):
        self.client = client
        self.dimensions = dimensions
        self.fingerprint = fingerprint

    def path(self, collection):
        if not re.fullmatch(r"sid_(?:kb|test)_[a-z0-9_]+", collection):
            raise ValueError("Only SID-owned reference/test collections are allowed")
        return f"/collections/{collection}"

    async def call(self, method, path, **kwargs):
        response = await self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()["result"]

    async def create(self, collection):
        # New generation only: never overwrite/recreate an existing collection.
        await self.call(
            "PUT",
            self.path(collection),
            json={
                "vectors": {"size": self.dimensions, "distance": "Cosine"},
                "metadata": {"sid_fingerprint": self.fingerprint},
            },
        )
        for field in ("record_id", "kind", "source"):
            await self.call(
                "PUT",
                self.path(collection) + "/index?wait=true",
                json={
                    "field_name": field,
                    "field_schema": "keyword",
                },
            )

    async def verify(self, collection):
        info = await self.call("GET", self.path(collection))
        params = info["config"]["params"]
        if (
            params["vectors"]["size"] != self.dimensions
            or params["vectors"]["distance"] != "Cosine"
            or info["config"].get("metadata", {}).get("sid_fingerprint") != self.fingerprint
        ):
            raise ValueError("Qdrant collection embedding contract mismatch")

    async def upsert(self, collection, points):
        for point in points:
            validate_vector(point["vector"], self.dimensions)
        if points:
            await self.call(
                "PUT", self.path(collection) + "/points?wait=true", json={"points": points}
            )

    async def delete_record(self, collection, record_id, *, through_version, keep_version=None):
        selection = {"must": [{"key": "record_id", "match": {"value": record_id}}]}
        selection["must"].append({"key": "version", "range": {"lte": through_version}})
        if keep_version is not None:
            selection["must_not"] = [{"key": "version", "match": {"value": keep_version}}]
        await self.call(
            "POST",
            self.path(collection) + "/points/delete?wait=true",
            json={
                "filter": selection,
            },
        )

    async def query(self, collection, vector, *, limit, source=None, kind=None):
        validate_vector(vector, self.dimensions)
        must = []
        for key, value in (("source", source), ("kind", kind)):
            if value:
                must.append({"key": key, "match": {"value": value}})
        response = await self.call(
            "POST",
            self.path(collection) + "/points/query",
            json={
                "query": vector,
                "limit": limit,
                "filter": {"must": must},
                "with_payload": True,
                "with_vector": False,
            },
        )
        return response["points"]

    async def scroll(self, collection, offset=None):
        return await self.call(
            "POST",
            self.path(collection) + "/points/scroll",
            json={
                "limit": 128,
                "offset": offset,
                "with_payload": True,
                "with_vector": False,
            },
        )

    async def delete_points(self, collection, ids):
        if ids:
            await self.call(
                "POST",
                self.path(collection) + "/points/delete?wait=true",
                json={
                    "points": ids,
                },
            )

    async def count(self, collection):
        return (
            await self.call(
                "POST",
                self.path(collection) + "/points/count",
                json={
                    "exact": True,
                },
            )
        )["count"]

    async def snapshot(self, collection):
        return await self.call("POST", self.path(collection) + "/snapshots")


def qdrant_client(settings):
    if not settings.qdrant_url:
        raise ValueError("Configure QDRANT_URL")
    key = settings.qdrant_api_key.get_secret_value()
    return httpx.AsyncClient(
        base_url=settings.qdrant_url.rstrip("/"),
        timeout=settings.qdrant_timeout_seconds,
        follow_redirects=False,
        headers={"api-key": key} if key else {},
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    )
