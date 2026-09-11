from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / "backend" / ".env")
MEETINGS_DIR = ROOT / "meetings"
CHUNK_SECONDS = float(os.getenv("AUDIO_CHUNK_SECONDS", "1.5"))
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base.en")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
