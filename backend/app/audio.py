from __future__ import annotations

import subprocess
import wave
from dataclasses import dataclass
from io import BytesIO

import numpy as np

from .config import FFMPEG_BIN


@dataclass
class DecodedAudio:
    samples: np.ndarray
    sample_rate: int = 16000


def decode_pcm(blob: bytes, sample_rate: int) -> DecodedAudio:
    """Decode raw mono int16 PCM sent directly from the browser extension."""
    if len(blob) < 2:
        raise RuntimeError("PCM chunk too small to decode")
    if len(blob) % 2:
        blob = blob[: len(blob) - 1]
    samples = np.frombuffer(blob, dtype=np.int16).astype(np.float32) / 32768.0
    if sample_rate == 16000:
        return DecodedAudio(samples=samples, sample_rate=16000)
    positions = np.linspace(0, len(samples) - 1, max(1, round(len(samples) * 16000 / sample_rate)))
    resampled = np.interp(positions, np.arange(len(samples)), samples).astype(np.float32)
    return DecodedAudio(samples=resampled, sample_rate=16000)


def decode_webm(blob: bytes) -> DecodedAudio:
    """Decode a browser MediaRecorder WebM/Opus blob without saving raw audio."""
    if len(blob) < 16:
        raise RuntimeError("audio chunk too small to decode")
    command = [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "webm",
        "-fflags",
        "+discardcorrupt",
        "-i",
        "pipe:0",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "s16le",
        "pipe:1",
    ]
    result = subprocess.run(command, input=blob, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        magic = blob[:4].hex()
        raise RuntimeError(
            f"ffmpeg audio decode failed (bytes={len(blob)}, magic={magic}): "
            f"{result.stderr.decode(errors='replace').strip()}"
        )
    samples = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    return DecodedAudio(samples=samples)


def read_wav(path: str) -> DecodedAudio:
    """Small dependency-free WAV reader used by the prerecorded pipeline test."""
    with wave.open(path, "rb") as wav:
        if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
            raise ValueError("test WAV must be mono 16-bit PCM")
        raw, rate = wav.readframes(wav.getnframes()), wav.getframerate()
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if rate == 16000:
        return DecodedAudio(data, rate)
    # Linear resampling is sufficient for test input; ffmpeg handles browser audio.
    positions = np.linspace(0, len(data) - 1, round(len(data) * 16000 / rate))
    return DecodedAudio(np.interp(positions, np.arange(len(data)), data).astype(np.float32), 16000)

