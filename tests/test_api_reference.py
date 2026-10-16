"""docs/api-reference.md must list every operation the service exposes."""

from pathlib import Path

from app.config import Settings
from app.main import create_app

REFERENCE = Path(__file__).resolve().parents[1] / "docs/api-reference.md"


def test_every_operation_is_documented():
    text = REFERENCE.read_text("utf-8")
    schema = create_app(Settings(_env_file=None)).openapi()
    missing = [
        f"{method.upper()} {path}"
        for path, item in schema["paths"].items()
        for method in item
        if f"| {method.upper()} | {path} |" not in text
    ]
    assert not missing, f"Add these to docs/api-reference.md: {missing}"
    assert f"API version **{schema['info']['version']}**" in text
