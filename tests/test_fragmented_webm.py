"""Simulate MediaRecorder timeslice fragments and verify decode with init prepend."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend"))

from app.audio import decode_webm

CLUSTER_ID = bytes([0x1F, 0x43, 0xB6, 0x75])


def split_webm(blob: bytes) -> tuple[bytes, list[bytes]]:
    cluster_at = blob.find(CLUSTER_ID)
    assert cluster_at > 0, "test webm must contain a Cluster element"
    init = blob[:cluster_at]
    media = blob[cluster_at:]
    mid = len(media) // 2
    return init, [media[:mid], media[mid:]]


def test_fragmented_webm_chunks(tmp_path: Path) -> None:
    webm = Path(__file__).parent / ".tmp" / "live_0.webm"
    if not webm.exists():
        import subprocess

        flac = Path(__file__).parent / ".tmp" / "jfk.flac"
        assert flac.exists(), "run integration test setup first"
        webm = tmp_path / "sample.webm"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                "0",
                "-t",
                "1.5",
                "-i",
                str(flac),
                "-c:a",
                "libopus",
                "-b:a",
                "64k",
                str(webm),
            ],
            check=True,
        )
    else:
        webm = webm

    blob = Path(webm).read_bytes()
    init, fragments = split_webm(blob)

    # First chunk with header decodes standalone.
    first = init + fragments[0]
    audio = decode_webm(first)
    assert len(audio.samples) > 0

    # Fragment-only chunk fails without init prepend.
    failed = False
    try:
        decode_webm(fragments[1])
    except RuntimeError:
        failed = True
    assert failed, "fragment-only chunk should fail without init segment"

    # Naive init prepend is unreliable (MediaRecorder splits mid-cluster).
    # v0.2.4+ uploads complete WebM files per chunk instead.
    repaired = init + fragments[1]
    try:
        decode_webm(repaired)
        prepend_works = True
    except RuntimeError:
        prepend_works = False
    assert not prepend_works, "init prepend should not be assumed safe; use complete-webm-per-chunk"
