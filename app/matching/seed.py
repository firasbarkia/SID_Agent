"""Load the synthetic platform dataset into demo collections only."""

import json
from pathlib import Path

from app.config import Settings

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / "data/contracts/platform-seed-v1.json"


async def seed_platform(db, settings: Settings, path=SEED) -> dict:
    targets = {
        "users": settings.platform_users_collection,
        "companies": settings.platform_companies_collection,
        "offers": settings.platform_offers_collection,
        "candidates": settings.platform_candidates_collection,
    }
    # The platform owns its real collections; SID only ever writes demo copies.
    if not all(name.startswith("demo_") for name in targets.values()):
        raise ValueError("Platform seed data may only be written to demo_* collections")
    data = json.loads(Path(path).read_text("utf-8"))
    counts = {}
    for key, collection in targets.items():
        for record in data[key]:
            await db[collection].replace_one({"id": record["id"]}, record, upsert=True)
        counts[key] = len(data[key])
    return counts
