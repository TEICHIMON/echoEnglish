"""FastAPI server for the Echo Loop web UI.

Run locally:
    uvicorn webapp.server:app --host 0.0.0.0 --port 8080

Endpoints:
    GET  /                       -> static SPA
    POST /api/jobs               -> create + enqueue a job
    GET  /api/jobs               -> history (newest first)
    GET  /api/jobs/{id}          -> status + outputs + log tail
    GET  /api/files/{id}/{name}  -> serve an output file (audio / lrc)
    DELETE /api/jobs/{id}        -> remove a job and its files

Optional shared-secret auth: set ``ECHO_WEB_TOKEN``. When set, every /api/*
request must carry it as ``Authorization: Bearer <token>`` or ``?token=``.
"""

from __future__ import annotations

import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from echo_logging import setup_logging
from webapp.jobs import VALID_MODES, manager

STATIC_DIR = Path(__file__).resolve().parent / "static"
ECHO_WEB_TOKEN = os.environ.get("ECHO_WEB_TOKEN", "").strip()


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv()  # OPENAI_API_KEY / GOOGLE_APPLICATION_CREDENTIALS etc.
    setup_logging()
    manager.start()
    yield


app = FastAPI(title="Echo Loop Generator", lifespan=lifespan)


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.query_params.get("token")


async def require_auth(request: Request) -> None:
    if not ECHO_WEB_TOKEN:
        return
    if _extract_token(request) != ECHO_WEB_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing token")


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------

class LoopOptions(BaseModel):
    variant: str | None = None
    tnt: int | None = None
    tst: int | None = None
    split: bool | None = None


class TimingOptions(BaseModel):
    after_first_target: float | None = None
    after_native: float | None = None
    after_second_target: float | None = None


class JobRequest(BaseModel):
    mode: str = Field(..., description="text | interview")
    content: str
    lang: str | None = None
    engine: str | None = None
    loop: LoopOptions | None = None
    timing: TimingOptions | None = None
    gain: float | None = None
    title: str | None = None


# --------------------------------------------------------------------------
# API routes
# --------------------------------------------------------------------------

@app.post("/api/jobs", dependencies=[Depends(require_auth)])
async def create_job(req: JobRequest):
    if req.mode not in VALID_MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {VALID_MODES}")
    request_payload = req.model_dump(exclude={"content", "mode"})
    try:
        job = manager.create(req.mode, req.content, request_payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"job_id": job.id}


@app.get("/api/jobs", dependencies=[Depends(require_auth)])
async def list_jobs():
    return {"jobs": manager.list()}


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_auth)])
async def get_job(job_id: str):
    payload = manager.status_payload(job_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="job not found")
    return payload


@app.delete("/api/jobs/{job_id}", dependencies=[Depends(require_auth)])
async def delete_job(job_id: str):
    if not manager.delete(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    return {"deleted": job_id}


@app.get("/api/files/{job_id}/{name}", dependencies=[Depends(require_auth)])
async def get_file(job_id: str, name: str, request: Request):
    path = manager.resolve_file(job_id, name)
    if path is None:
        raise HTTPException(status_code=404, detail="file not found")
    media_type, _ = mimetypes.guess_type(path.name)
    suffix = path.suffix.lower()
    if suffix == ".lrc":
        media_type = "text/plain; charset=utf-8"
    elif suffix == ".m4a":
        # mimetypes guesses audio/mp4a-latm, which some browsers won't play in
        # an <audio> element. audio/mp4 is the broadly-supported type.
        media_type = "audio/mp4"
    download = request.query_params.get("download") is not None
    headers = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
    return FileResponse(path, media_type=media_type or "application/octet-stream", headers=headers)


@app.get("/api/config", dependencies=[Depends(require_auth)])
async def get_config():
    """Tiny config surface for the UI (auth requirement only, for now)."""
    return {"auth_required": bool(ECHO_WEB_TOKEN)}


# --------------------------------------------------------------------------
# Static frontend
# --------------------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
async def healthz():
    return JSONResponse({"ok": True})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
