"""Local simulation lifecycle: python -m scripts.simulation start|stop|status|check."""

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil

from app.config import Settings
from app.demo import DEMO_SERVICE_KEY, ROOT, create_demo_app

ARTIFACTS = ROOT / ".artifacts/simulation"
STATE = ARTIFACTS / "processes.json"
API = "http://127.0.0.1:8001"
UI = "http://127.0.0.1:8501"
HEADERS = {"X-Service-Key": DEMO_SERVICE_KEY, "X-Demo-User": "demo-candidate"}


def simulation_settings():
    # Supply every field explicitly: neither .env nor inherited provider/database
    # settings can redirect this launcher to a real account or platform database.
    values = {name: field.get_default() for name, field in Settings.model_fields.items()}
    values.update(
        mongodb_uri="mongodb://127.0.0.1:27018/?replicaSet=rs0&directConnection=true",
        qdrant_url="http://127.0.0.1:6335",
        embedding_model_directory=str(ROOT / ".models/multilingual-minilm"),
    )
    return Settings(_env_file=None, **values)


def create_app():
    return create_demo_app(simulation_settings(), live=False)


def saved_process(record):
    """PID reuse must never let stop terminate an unrelated process."""
    try:
        process = psutil.Process(record["pid"])
        if (
            process.create_time() == record["created"]
            and process.cmdline() == record["command"]
            and Path(process.cwd()).resolve() == ROOT
        ):
            return process
    except (psutil.Error, KeyError, OSError):
        pass
    return None


def read_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def port_free(port):
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def wait_http(url, process=None, timeout=60):
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=2, trust_env=False) as client:
        while time.monotonic() < deadline:
            if process and not process.is_running():
                raise RuntimeError(f"Service exited. Inspect logs in {ARTIFACTS}")
            try:
                response = client.get(url)
                if response.status_code == 200:
                    return response
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
    raise RuntimeError(f"Service did not become ready: {url}. Inspect {ARTIFACTS}")


def launch(name, arguments):
    environment = dict(os.environ, SID_DEMO_LIVE_AI="0", SID_DEMO_API_URL=API)
    options = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    with (ARTIFACTS / f"{name}.log").open("ab") as log:
        child = subprocess.Popen(
            [sys.executable, *arguments],
            cwd=ROOT,
            env=environment,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            **options,
        )
    process = psutil.Process(child.pid)
    return {
        "pid": child.pid,
        "created": process.create_time(),
        "command": process.cmdline(),
    }


def start():
    if not (ROOT / ".models/multilingual-minilm/contract.json").is_file():
        raise RuntimeError("First run: uv run python -m app.knowledge.cli download-model")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    state = read_state()
    for name, port in (("api", 8001), ("ui", 8501)):
        if not saved_process(state.get(name, {})) and not port_free(port):
            raise RuntimeError(f"Port {port} is occupied by an unmanaged service. Stop it first.")
    for project, file, port in (
        ("sid-phase2", "compose.mongodb.yml", 27018),
        ("sid-phase3", "compose.qdrant.yml", 6335),
    ):
        if port_free(port):
            subprocess.run(
                ["docker", "compose", "-p", project, "-f", file, "up", "-d", "--wait"],
                cwd=ROOT,
                check=True,
            )
    wait_http("http://127.0.0.1:6335/readyz")
    if not saved_process(state.get("api", {})):
        state["api"] = launch(
            "api",
            [
                "-m",
                "uvicorn",
                "scripts.simulation:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                "8001",
                "--no-proxy-headers",
            ],
        )
        STATE.write_text(json.dumps(state, indent=2))
    response = wait_http(API + "/demo/status", saved_process(state["api"]))
    if response.json().get("ai_mode") != "simulated":
        raise RuntimeError("Expected simulated AI; refusing to continue")
    print("Preparing local reference index...", flush=True)
    with httpx.Client(headers=HEADERS, timeout=210, trust_env=False) as client:
        ready = client.post(API + "/demo/knowledge/setup")
        ready.raise_for_status()
    if not saved_process(state.get("ui", {})):
        state["ui"] = launch(
            "ui",
            [
                "-m",
                "streamlit",
                "run",
                "streamlit_app.py",
                "--server.address",
                "127.0.0.1",
                "--server.port",
                "8501",
                "--server.headless",
                "true",
                "--browser.gatherUsageStats",
                "false",
            ],
        )
        STATE.write_text(json.dumps(state, indent=2))
    wait_http(UI + "/_stcore/health", saved_process(state["ui"]))
    print(f"Simulation ready: {UI}\nAPI docs: {API}/docs\nLogs: {ARTIFACTS}")


def stop():
    for name, record in read_state().items():
        process = saved_process(record)
        if process:
            # Windows venv python.exe launches a child interpreter. Stopping
            # only that parent would leave the server and its port running.
            owned = [*reversed(process.children(recursive=True)), process]
            for child in owned:
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            _, alive = psutil.wait_procs(owned, timeout=15)
            if alive:
                raise RuntimeError(f"Simulation {name} did not stop; process record retained")
            print(f"Stopped simulation {name}")
    STATE.unlink(missing_ok=True)
    print("Demo records and shared MongoDB/Qdrant services retained.")


def status():
    state = read_state()
    for name, url in (("api", API), ("ui", UI)):
        running = bool(saved_process(state.get(name, {})))
        print(f"{name}: {'running' if running else 'stopped'} ({url})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status", "check"])
    args = parser.parse_args()
    try:
        if args.action == "check":
            from examples.simulation_checks import run_checks

            report = asyncio.run(run_checks(API))
            ARTIFACTS.mkdir(parents=True, exist_ok=True)
            target = ARTIFACTS / "latest-checks.json"
            target.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            for check in report["checks"]:
                print(f"{check['status'].upper()}: {check['scenario']}")
            print(f"Report: {target}")
            return 0 if report["passed"] else 1
        {"start": start, "stop": stop, "status": status}[args.action]()
    except (
        RuntimeError,
        OSError,
        httpx.HTTPError,
        subprocess.SubprocessError,
        psutil.Error,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
