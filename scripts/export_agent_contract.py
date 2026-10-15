"""Export schemas without starting services, reading source data or calling AI."""

import argparse
import json
from pathlib import Path

from app.config import Settings
from app.contracts.models import AcceptedProfileSync
from app.main import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path(".artifacts/agent-contracts"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    schema = create_app(Settings(_env_file=None)).openapi()
    (args.out / "agent-v1.openapi.json").write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.out / "profile-sync-v1.schema.json").write_text(
        json.dumps(AcceptedProfileSync.model_json_schema(), indent=2) + "\n", encoding="utf-8"
    )
    print(f"Exported agent-v1 and proposed profile-sync-v1 schemas to {args.out}")


if __name__ == "__main__":
    main()
