from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from .audio import DecodedAudio, decode_pcm, decode_webm
from .config import MEETINGS_DIR, WHISPER_COMPUTE_TYPE, WHISPER_MODEL
from .diarization import LocalSpeakerTracker
from .transcript import TranscriptWriter

log = logging.getLogger(__name__)

SILENT_RMS_THRESHOLD = 0.003


def deduplicate(previous: str, current: str) -> str:
    """Remove a shared word overlap between adjacent independently decoded chunks."""
    old, new = previous.split(), current.split()
    for size in range(min(len(old), len(new)), 0, -1):
        if [word.lower() for word in old[-size:]] == [word.lower() for word in new[:size]]:
            return " ".join(new[size:])
    return current


@dataclass
class SessionDebug:
    chunks_received: int = 0
    chunks_decoded: int = 0
    chunks_silent: int = 0
    chunks_no_speech: int = 0
    chunks_written: int = 0
    chunks_failed: int = 0
    extension_events: list[str] = field(default_factory=list)
    last_chunk: dict | None = None
    last_error: str | None = None

    def as_dict(self) -> dict:
        return {
            "chunks_received": self.chunks_received,
            "chunks_decoded": self.chunks_decoded,
            "chunks_silent": self.chunks_silent,
            "chunks_no_speech": self.chunks_no_speech,
            "chunks_written": self.chunks_written,
            "chunks_failed": self.chunks_failed,
            "last_chunk": self.last_chunk,
            "last_error": self.last_error,
            "extension_events": self.extension_events[-8:],
        }


class Transcriber:
    def __init__(self) -> None:
        self.model = None

    def transcribe(self, audio: DecodedAudio) -> str:
        if self.model is None:
            from faster_whisper import WhisperModel
            log.info("loading whisper model=%s device=cpu compute=%s", WHISPER_MODEL, WHISPER_COMPUTE_TYPE)
            self.model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type=WHISPER_COMPUTE_TYPE)
        segments, _ = self.model.transcribe(
            audio.samples,
            language="en",
            vad_filter=True,
            beam_size=1,
            condition_on_previous_text=False,
            no_speech_threshold=0.35,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()


class MeetingService:
    def __init__(self) -> None:
        self.active = False
        self.started: datetime | None = None
        self.writer: TranscriptWriter | None = None
        self.tracker = LocalSpeakerTracker()
        self.transcriber = Transcriber()
        self.last_text = ""
        self.lock = asyncio.Lock()
        self.metrics: dict[str, float | int | str] = {}
        self.debug = SessionDebug()

    def start(self) -> Path:
        if self.active:
            raise ValueError("a meeting is already transcribing")
        self.started = datetime.now()
        path = MEETINGS_DIR / f"meeting_{self.started:%Y-%m-%d_%H-%M-%S}.md"
        self.writer = TranscriptWriter(path, self.started)
        self.tracker = LocalSpeakerTracker()
        self.last_text = ""
        self.active = True
        self.metrics = {}
        self.debug = SessionDebug()
        log.info("meeting started transcript=%s", path.name)
        return path

    def log_extension_event(self, event: str, detail: str = "") -> None:
        line = f"{event}: {detail}".strip(": ")
        self.debug.extension_events.append(line)
        log.info("extension | %s", line)

    async def ingest(self, blob: bytes, sample_rate: int | None = None) -> None:
        if not self.active or not self.writer or not self.started:
            raise ValueError("no active meeting")
        async with self.lock:
            self.debug.chunks_received += 1
            chunk_no = self.debug.chunks_received
            received = time.time()
            decode_format = f"pcm@{sample_rate}" if sample_rate else "webm"
            magic = blob[:4].hex() if len(blob) >= 4 else "n/a"

            try:
                if sample_rate:
                    log.warning(
                        "#%-3d OUTDATED extension sent PCM — reload extension at brave://extensions (expect webm chunks)",
                        chunk_no,
                    )
                    audio = await asyncio.to_thread(decode_pcm, blob, sample_rate)
                else:
                    audio = await asyncio.to_thread(decode_webm, blob)
                self.debug.chunks_decoded += 1

                duration_s = len(audio.samples) / audio.sample_rate if audio.sample_rate else 0.0
                rms = float(np.sqrt(np.mean(audio.samples ** 2))) if len(audio.samples) else 0.0
                peak = float(np.max(np.abs(audio.samples))) if len(audio.samples) else 0.0

                began = time.time()
                raw_text = await asyncio.to_thread(self.transcriber.transcribe, audio)
                completed = time.time()
                text = deduplicate(self.last_text, raw_text).strip()

                outcome = "written"
                if rms < SILENT_RMS_THRESHOLD:
                    self.debug.chunks_silent += 1
                    outcome = "silent"
                elif not raw_text.strip():
                    self.debug.chunks_no_speech += 1
                    outcome = "no_speech"
                elif not text:
                    outcome = "deduped_empty"

                if text:
                    speaker = self.tracker.label(audio.samples)
                    self.writer.append(time.time() - self.started.timestamp(), speaker, text)
                    self.last_text = raw_text
                    self.debug.chunks_written += 1

                written = time.time()
                latency = written - received
                self.metrics = {
                    "chunk_number": chunk_no,
                    "audio_received_at": received,
                    "transcription_started_at": began,
                    "transcription_completed_at": completed,
                    "transcript_written_at": written,
                    "processing_seconds": completed - began,
                    "end_to_end_seconds": latency,
                    "chunk_bytes": len(blob),
                    "decode_format": decode_format,
                    "audio_rms": round(rms, 5),
                    "audio_peak": round(peak, 5),
                    "audio_seconds": round(duration_s, 2),
                    "raw_text_chars": len(raw_text),
                    "text_chars": len(text),
                    "outcome": outcome,
                }
                self.debug.last_chunk = dict(self.metrics)
                self.debug.last_error = None

                log.info(
                    "#%-3d %-12s bytes=%-5d magic=%s audio=%.2fs rms=%.4f peak=%.4f raw=%r out=%s wrote=%s latency=%.2fs",
                    chunk_no,
                    decode_format,
                    len(blob),
                    magic,
                    duration_s,
                    rms,
                    peak,
                    (raw_text[:60] + "…") if len(raw_text) > 60 else raw_text,
                    outcome,
                    bool(text),
                    latency,
                )
                if outcome == "silent":
                    log.warning(
                        "#%-3d SILENT chunk (rms=%.5f) — tab audio likely not reaching the extension",
                        chunk_no,
                        rms,
                    )
                elif outcome == "no_speech":
                    log.warning(
                        "#%-3d audible chunk but Whisper heard nothing (rms=%.4f) — speak louder or check mic in Meet",
                        chunk_no,
                        rms,
                    )
            except Exception as error:
                self.debug.chunks_failed += 1
                self.debug.last_error = str(error)
                log.warning(
                    "#%-3d FAILED %-12s bytes=%d magic=%s error=%s",
                    chunk_no,
                    decode_format,
                    len(blob),
                    magic,
                    error,
                )

    async def stop(self) -> Path | None:
        async with self.lock:
            self.active = False
            if self.writer:
                log.info(
                    "meeting stopped transcript=%s received=%d written=%d silent=%d failed=%d",
                    self.writer.path.name,
                    self.debug.chunks_received,
                    self.debug.chunks_written,
                    self.debug.chunks_silent,
                    self.debug.chunks_failed,
                )
            return self.writer.path if self.writer else None

    def status(self) -> dict:
        duration = time.time() - self.started.timestamp() if self.active and self.started else 0
        return {
            "status": "transcribing" if self.active else "stopped",
            "duration_seconds": int(duration),
            "transcript": self.writer.path.name if self.writer else None,
            "metrics": self.metrics,
            "debug": self.debug.as_dict(),
        }
