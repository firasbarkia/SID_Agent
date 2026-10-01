"""uv run python -m app.knowledge.cli --help"""

import argparse
import asyncio
import json
from contextlib import suppress
from pathlib import Path

import httpx
from pymongo.errors import PyMongoError

from app.config import Settings
from app.db.connection import ensure_indexes, mongo_connection
from app.knowledge.embedding import DIMENSIONS, FINGERPRINT, LocalEmbedder, download_model
from app.knowledge.normalize import normalize
from app.knowledge.qdrant import QdrantIndex, qdrant_client
from app.knowledge.store import KnowledgeStore
from app.knowledge.worker import IndexWorker


def parser():
    parser = argparse.ArgumentParser(description="Public reference knowledge base lifecycle")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("download-model", help="Explicitly download pinned ONNX/tokenizer files")
    for name in ("normalize", "import"):
        command = commands.add_parser(name)
        command.add_argument(
            "--snapshot", type=Path, default=Path("data/knowledge_base/2026-09-29")
        )
        command.add_argument("--report", type=Path, default=Path(".artifacts/kb-manifest.json"))
    commands.add_parser("rebuild", help="Create and seed a new generation; retain the old index")
    seed = commands.add_parser("seed", help="Resume seeding after an interrupted rebuild")
    seed.add_argument("generation")
    commands.add_parser("status")
    worker = commands.add_parser("worker", help="Run a separately scalable indexing worker")
    worker.add_argument("--drain", action="store_true", help="Exit when no claim is available")
    for name in ("reconcile", "promote", "snapshot", "retry-failed"):
        command = commands.add_parser(name)
        command.add_argument("generation")
    for name in ("revoke", "delete"):
        command = commands.add_parser(name)
        command.add_argument("record_id")
    return parser


async def main(args):
    settings = Settings()
    if args.command == "download-model":
        await asyncio.to_thread(download_model, Path(settings.embedding_model_directory))
        print(json.dumps({"fingerprint": FINGERPRINT, "dimensions": DIMENSIONS}))
        return
    if args.command in {"normalize", "import"}:
        snapshot = normalize(args.snapshot)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(snapshot.manifest(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if snapshot.rejected:
            raise ValueError("Rejected rows: review the report; import has not started")
        if args.command == "normalize":
            print(json.dumps(snapshot.manifest()["counts"]))
            return
    async with mongo_connection(settings) as db:
        if db is None:
            raise ValueError("Configure MONGODB_URI")
        await ensure_indexes(db)
        store = KnowledgeStore(db)
        if args.command == "import":
            result = await store.import_snapshot(snapshot)
            print(json.dumps({k: result[k] for k in ("changed", "total")}))
            return
        if args.command in {"revoke", "delete"}:
            await store.set_visibility(
                args.record_id,
                **({"public": False} if args.command == "revoke" else {"deleted": True}),
            )
            return
        if args.command == "status":
            print(
                json.dumps(
                    {
                        "records": await db.sid_kb_records.count_documents({}),
                        "catalog": await db.sid_kb_catalog.find_one({"_id": "catalog"}),
                        "generations": await db.sid_kb_generations.find({}).to_list(),
                        "jobs": {
                            state: await db.sid_index_jobs.count_documents({"state": state})
                            for state in ("pending", "running", "failed", "done")
                        },
                    },
                    default=str,
                )
            )
            return
        if args.command == "retry-failed":
            # Explicit operator retry after fixing the cause; never auto-loop failed work.
            result = await db.sid_index_jobs.update_many(
                {"generation": args.generation, "state": "failed"},
                {"$set": {"state": "pending", "attempts": 0, "error_code": None}},
            )
            print(json.dumps({"reset": result.modified_count}))
            return
        async with qdrant_client(settings) as client:
            index = QdrantIndex(client, DIMENSIONS, FINGERPRINT)
            if args.command == "rebuild":
                generation = await store.begin_generation(FINGERPRINT, DIMENSIONS)
                print(json.dumps({"generation": generation}), flush=True)
                await index.create(generation)
                await store.seed_generation(generation)
            elif args.command == "seed":
                meta = await db.sid_kb_generations.find_one(
                    {
                        "_id": args.generation,
                        "state": "building",
                        "fingerprint": FINGERPRINT,
                    }
                )
                if meta is None:
                    raise ValueError("Only a matching building generation can be resumed")
                try:
                    await index.verify(args.generation)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 404:
                        raise
                    await index.create(args.generation)
                await store.seed_generation(args.generation)
            elif args.command == "promote":
                await store.promote(args.generation, index)
            elif args.command == "snapshot":
                await index.verify(args.generation)
                print(json.dumps(await index.snapshot(args.generation)))
            elif args.command in {"worker", "reconcile"}:
                embedder = await asyncio.to_thread(
                    LocalEmbedder, settings.embedding_model_directory, settings.embedding_threads
                )
                worker = IndexWorker(db, index, embedder, settings)
                if args.command == "reconcile":
                    print(json.dumps({"deleted_points": await worker.reconcile(args.generation)}))
                    return
                while True:
                    try:
                        if not await worker.run_once():
                            if args.drain:
                                return
                            await asyncio.sleep(settings.worker_poll_seconds)
                    except PyMongoError:
                        if args.drain:
                            raise
                        await asyncio.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        asyncio.run(main(parser().parse_args()))
