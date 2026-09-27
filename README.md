# SID Agent API

A minimal FastAPI project using Python 3.11+ and uv.

## Setup

```powershell
uv sync
```

## Run locally

```powershell
uv run fastapi dev
```

The development server runs at http://127.0.0.1:8000 with automatic reload.

- Interactive API documentation: http://127.0.0.1:8000/docs
- Alternative documentation: http://127.0.0.1:8000/redoc
- Health check: http://127.0.0.1:8000/health

Application routes are defined in `app/main.py`. Dependencies are declared in
`pyproject.toml` and locked in `uv.lock`.

For a server without development reload, run `uv run fastapi run`.
