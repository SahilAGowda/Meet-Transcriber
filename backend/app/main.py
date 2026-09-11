from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .service import MeetingService
from .config import CHUNK_SECONDS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

app = FastAPI(title="Local Google Meet Transcriber")
app.add_middleware(CORSMiddleware, allow_origins=["chrome-extension://*"], allow_methods=["*"], allow_headers=["*"])
service = MeetingService()


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/config")
def config() -> dict:
    return {"audio_chunk_milliseconds": max(250, round(CHUNK_SECONDS * 1000))}


@app.post("/meeting/start")
def start() -> dict:
    try:
        path = service.start()
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    log.info("api POST /meeting/start -> %s", path.name)
    return {"transcript": path.name}


@app.post("/meeting/audio", status_code=202)
async def audio(
    chunk: UploadFile = File(...),
    sample_rate: int | None = Form(None),
) -> dict:
    blob = await chunk.read()
    if not blob:
        raise HTTPException(400, "empty audio chunk")
    try:
        await service.ingest(blob, sample_rate)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return {"accepted": True}


@app.post("/meeting/stop")
async def stop() -> dict:
    path = await service.stop()
    log.info("api POST /meeting/stop -> %s", path.name if path else None)
    return {"transcript": path.name if path else None}


@app.post("/meeting/debug/extension")
def extension_debug(payload: dict = Body(...)) -> dict:
    event = str(payload.get("event", "unknown"))
    detail = str(payload.get("detail", ""))
    service.log_extension_event(event, detail)
    return {"ok": True}


@app.get("/meeting/status")
def status() -> dict:
    return service.status()


@app.get("/meeting/transcript/path")
def transcript_path() -> dict:
    if not service.writer:
        raise HTTPException(404, "no transcript")
    return {"path": str(service.writer.path.resolve()), "name": service.writer.path.name}


@app.get("/meeting/transcript")
def transcript() -> FileResponse:
    if not service.writer or not service.writer.path.exists():
        raise HTTPException(404, "no transcript")
    return FileResponse(service.writer.path, media_type="text/markdown", filename=service.writer.path.name)
