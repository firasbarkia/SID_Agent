"""Exercise Git's checkout conversion, not just the present working copy."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("autocrlf", ["true", "false"])
def test_fresh_checkout_preserves_download_hashes(tmp_path, autocrlf):
    root = Path(__file__).resolve().parents[1]
    relative = Path("data/knowledge_base/2026-09-29")
    source = root / relative
    checks = json.loads((source / "availability_checks.json").read_text("utf-8"))
    repository = tmp_path / "repository"
    repository.mkdir()
    shutil.copyfile(root / ".gitattributes", repository / ".gitattributes")
    shutil.copytree(source / "raw", repository / relative / "raw")
    # Do not inherit an alternate index/worktree from the caller.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    def git(*args):
        subprocess.run(
            ["git", "-C", str(repository), *args],
            env=env,
            check=True,
            capture_output=True,
            timeout=30,
        )

    git("init", "-q")
    git("config", "core.autocrlf", autocrlf)
    git("add", ".")
    checkout = tmp_path / "checkout"
    git("checkout-index", "--all", "--prefix=" + checkout.as_posix() + "/")
    for check in checks:
        if check.get("local_file"):
            path = checkout / check["local_file"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == check["sha256"], path.name
