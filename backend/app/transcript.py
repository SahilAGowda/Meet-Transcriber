from __future__ import annotations

from datetime import datetime
from pathlib import Path


class TranscriptWriter:
    def __init__(self, path: Path, started: datetime) -> None:
        self.path, self.started = path, started
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# Google Meet Transcript\n\nStarted: {started:%Y-%m-%d %H:%M:%S}\n\n", encoding="utf-8")

    def append(self, seconds: float, speaker: str, text: str) -> None:
        minutes, remainder = divmod(max(0, int(seconds)), 60)
        line = f"[{minutes:02d}:{remainder:02d}] {speaker}:\n{text.strip()}\n\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()

