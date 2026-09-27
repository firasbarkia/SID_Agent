from fastapi import FastAPI

app = FastAPI(title="SID Agent API", version="0.1.0")


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "Welcome to SID Agent API"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
