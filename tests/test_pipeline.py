import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend"))

from app.service import deduplicate
from app.transcript import TranscriptWriter
from datetime import datetime


def test_removes_word_overlap():
    assert deduplicate("Let's discuss the database", "database schema first") == "schema first"


def test_writer_flushes_markdown(tmp_path):
    path = tmp_path / "meeting.md"
    writer = TranscriptWriter(path, datetime(2026, 9, 1, 16, 30, 21))
    writer.append(3, "Speaker 1", "Good morning")
    assert "[00:03] Speaker 1:\nGood morning" in path.read_text()

