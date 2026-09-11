#!/usr/bin/env python3
"""
pipewire_loopback_capture.py — Capture Brave/Meet audio via PipeWire sink monitor.

This script bypasses all browser-extension microphone permission problems by
capturing audio directly from the PipeWire sink monitor that Brave routes its
audio to.  It segments the stream into 1.5-second chunks and POSTs each chunk
as a complete WebM (Opus) file to the backend's /meeting/audio endpoint, exactly
the same format the browser extension sends.

Usage:
    python3 scripts/pipewire_loopback_capture.py [--backend http://127.0.0.1:8000]
                                                   [--chunk-ms 1500]
                                                   [--sink-name <pw-sink-name>]
                                                   [--list-sinks]

The script will auto-detect the active sink if --sink-name is not provided.
It will capture from the highest-priority sink (prefers Bluetooth > Speakers > HDMI).

Strategy: Use pw-cat (PipeWire native record) piped into ffmpeg for WebM encoding.
This works for both ALSA sinks and Bluetooth sinks, unlike 'ffmpeg -f pulse'.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import subprocess
import sys
import tempfile
import time
import threading
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("loopback")

BACKEND_DEFAULT = "http://127.0.0.1:8000"
CHUNK_MS_DEFAULT = 1500
FFMPEG_BIN = "ffmpeg"

# Sink scoring: highest wins
# Bluetooth is highest priority because it's often the active output
SINK_PRIORITY_KEYWORDS = [
    ("bluez", 30),          # Bluetooth headphones/earbuds — almost always the active output
    ("bluetooth", 30),
    ("speaker", 10),
    ("headphone", 10),
    ("headset", 10),
    ("analog", 5),
    ("sofhdadsp__sink", 5), # Built-in speakers
]
SINK_DEPRIORITIZE_KEYWORDS = [
    ("hdmi", -5),
    ("dp", -3),
    ("displayport", -5),
]

_stop_event = threading.Event()


# ---------------------------------------------------------------------------
# PipeWire sink discovery
# ---------------------------------------------------------------------------

def get_pipewire_nodes() -> list[dict]:
    """Return all PipeWire nodes as a list of dicts from pw-dump."""
    try:
        result = subprocess.run(
            ["pw-dump"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=10,
        )
        return json.loads(result.stdout)
    except FileNotFoundError:
        log.error("pw-dump not found. Install pipewire-bin:  sudo apt install pipewire-bin")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        log.error("pw-dump timed out.")
        sys.exit(1)
    except json.JSONDecodeError as exc:
        log.error("Failed to parse pw-dump output: %s", exc)
        sys.exit(1)


def find_audio_sinks(nodes: list[dict]) -> list[dict]:
    """Return Audio/Sink nodes with their PipeWire node ID."""
    sinks = []
    for node in nodes:
        if node.get("type") != "PipeWire:Interface:Node":
            continue
        info = node.get("info", {})
        props = info.get("props", {})
        if props.get("media.class") == "Audio/Sink":
            node_name = props.get("node.name", "")
            sinks.append({
                "id": node["id"],
                "name": node_name,
                "description": props.get("node.description", props.get("device.description", "")),
                # pw-cat target is the node name (NOT .monitor suffix — pw-cat handles that automatically)
                "pw_target": node_name,
            })
    return sinks


def score_sink(sink: dict) -> int:
    """Higher score = preferred for capture."""
    name_lower = (sink["name"] + " " + sink["description"]).lower()
    score = 0
    for kw, pts in SINK_PRIORITY_KEYWORDS:
        if kw in name_lower:
            score += pts
    for kw, pts in SINK_DEPRIORITIZE_KEYWORDS:
        if kw in name_lower:
            score += pts  # pts are already negative
    return score


def choose_sink(sinks: list[dict], preferred_name: str | None = None) -> dict | None:
    """Pick the best sink or match preferred_name."""
    if not sinks:
        return None
    if preferred_name:
        for s in sinks:
            if preferred_name in s["name"] or preferred_name in s["description"]:
                return s
        log.warning("Specified sink '%s' not found; falling back to auto-select.", preferred_name)
    return max(sinks, key=score_sink)


# ---------------------------------------------------------------------------
# Audio capture using pw-cat → ffmpeg pipeline
# ---------------------------------------------------------------------------

def capture_loop(pw_target: str, backend: str, chunk_ms: int) -> None:
    """
    Run pw-cat (PipeWire native record) targeting the sink monitor, pipe raw PCM
    into ffmpeg which segments it into complete WebM/Opus chunks and writes them
    to a temp directory.  Each completed chunk is POSTed to /meeting/audio.

    pw-cat with --target <sink-name> automatically records from the sink's
    monitor (loopback), capturing whatever audio that sink plays — including
    Bluetooth headphone output.
    """
    log.info("Starting loopback capture from PipeWire sink: %s", pw_target)
    log.info("Chunk duration: %d ms  →  backend: %s", chunk_ms, backend)

    chunk_secs = chunk_ms / 1000.0
    seq = 0
    sample_rate = 48000
    channels = 1

    with tempfile.TemporaryDirectory(prefix="gmeet_loopback_") as tmpdir:
        tmpdir_path = Path(tmpdir)
        segment_pattern = str(tmpdir_path / "chunk_%05d.webm")

        # pw-cat command: record from the PipeWire sink monitor as raw s16 PCM
        pwcat_cmd = [
            "pw-cat",
            "--record",
            "--target", pw_target,
            "--channels", str(channels),
            "--rate", str(sample_rate),
            "--format", "s16",       # signed 16-bit little-endian
            "-",                     # pipe to stdout
        ]

        # ffmpeg reads raw PCM from stdin and writes segmented WebM files
        ffmpeg_cmd = [
            FFMPEG_BIN,
            "-hide_banner",
            "-loglevel", "error",
            # Raw PCM input from pw-cat
            "-f", "s16le",
            "-ac", str(channels),
            "-ar", str(sample_rate),
            "-i", "pipe:0",
            # Encode to Opus at 64k
            "-c:a", "libopus",
            "-b:a", "64k",
            # Segment muxer: one complete WebM per segment
            "-f", "segment",
            "-segment_time", str(chunk_secs),
            "-segment_format", "webm",
            "-reset_timestamps", "1",
            "-strftime", "0",
            segment_pattern,
        ]

        log.info("pw-cat → ffmpeg pipeline starting…")

        # Start pw-cat
        try:
            pwcat_proc = subprocess.Popen(
                pwcat_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            log.error("pw-cat not found. Install: sudo apt install pipewire-audio-client-libraries")
            sys.exit(1)

        # Start ffmpeg reading from pw-cat's stdout
        try:
            ffmpeg_proc = subprocess.Popen(
                ffmpeg_cmd,
                stdin=pwcat_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            log.error("ffmpeg not found. Install: sudo apt install ffmpeg")
            pwcat_proc.terminate()
            sys.exit(1)

        # Allow pw-cat to write to ffmpeg's stdin
        pwcat_proc.stdout.close()

        # Log stderr from both processes
        def _log_stderr(proc, name):
            for line in proc.stderr:
                stripped = line.decode(errors="replace").rstrip()
                if stripped:
                    log.warning("%s: %s", name, stripped)

        threading.Thread(target=_log_stderr, args=(pwcat_proc, "pw-cat"), daemon=True).start()
        threading.Thread(target=_log_stderr, args=(ffmpeg_proc, "ffmpeg"), daemon=True).start()

        uploaded_index = 0
        last_chunk_time = time.time()

        try:
            while not _stop_event.is_set():
                # Find completed segments
                all_chunks = sorted(tmpdir_path.glob("chunk_*.webm"))
                # The highest-index chunk is still being written by ffmpeg; skip it
                ready_chunks = all_chunks[:-1] if len(all_chunks) > 1 else []

                for chunk_path in ready_chunks:
                    idx_str = chunk_path.stem.split("_")[1]
                    chunk_index = int(idx_str)
                    if chunk_index < uploaded_index:
                        chunk_path.unlink(missing_ok=True)
                        continue

                    blob = chunk_path.read_bytes()
                    chunk_path.unlink(missing_ok=True)
                    uploaded_index = chunk_index + 1
                    seq += 1
                    last_chunk_time = time.time()

                    if not blob:
                        log.debug("chunk_%05d empty, skipping", chunk_index)
                        continue

                    _upload_chunk(blob, seq, backend)

                # Watchdog: if no chunk appears for 10s something is wrong
                if time.time() - last_chunk_time > 10:
                    log.warning(
                        "No chunk produced in 10s. pw-cat may have lost audio routing. "
                        "Check if your headphones are still connected."
                    )
                    last_chunk_time = time.time()

                if pwcat_proc.poll() is not None:
                    log.error("pw-cat exited unexpectedly (code %d)", pwcat_proc.returncode)
                    break
                if ffmpeg_proc.poll() is not None:
                    log.error("ffmpeg exited unexpectedly (code %d)", ffmpeg_proc.returncode)
                    break

                time.sleep(0.1)

        finally:
            pwcat_proc.terminate()
            ffmpeg_proc.terminate()
            try:
                pwcat_proc.wait(timeout=3)
                ffmpeg_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pwcat_proc.kill()
                ffmpeg_proc.kill()
            log.info("Loopback capture stopped. Chunks uploaded: %d", seq)


def _upload_chunk(blob: bytes, seq: int, backend: str) -> None:
    """POST a single WebM chunk to /meeting/audio."""
    try:
        resp = requests.post(
            f"{backend}/meeting/audio",
            files={"chunk": ("loopback.webm", blob, "audio/webm")},
            timeout=10,
        )
        if resp.status_code == 202:
            log.debug("chunk %d → 202 Accepted (%d bytes)", seq, len(blob))
        elif resp.status_code == 409:
            log.warning("chunk %d → 409 Conflict (no active meeting — click Start in the popup)", seq)
        else:
            log.warning("chunk %d → HTTP %d: %s", seq, resp.status_code, resp.text[:200])
    except requests.exceptions.ConnectionError:
        log.warning("chunk %d → backend unreachable (%s)", seq, backend)
    except requests.exceptions.Timeout:
        log.warning("chunk %d → upload timed out", seq)


# ---------------------------------------------------------------------------
# Wait for an active meeting session
# ---------------------------------------------------------------------------

def wait_for_meeting(backend: str, poll_interval: float = 2.0) -> bool:
    """Block until the backend reports status=transcribing or user interrupts."""
    log.info("Waiting for meeting to start — click 'Start Transcription' in the extension popup…")
    while not _stop_event.is_set():
        try:
            resp = requests.get(f"{backend}/meeting/status", timeout=5)
            if resp.ok:
                data = resp.json()
                if data.get("status") == "transcribing":
                    log.info("✓ Meeting active: %s", data.get("transcript", "?"))
                    return True
        except requests.exceptions.ConnectionError:
            log.warning("Backend not reachable yet — retrying…")
        except requests.exceptions.Timeout:
            pass
        time.sleep(poll_interval)
    return False


def check_backend_health(backend: str) -> bool:
    try:
        resp = requests.get(f"{backend}/health", timeout=5)
        return resp.ok
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture PipeWire audio and stream to the local Meet transcriber backend."
    )
    parser.add_argument("--backend", default=BACKEND_DEFAULT, help="Backend URL")
    parser.add_argument("--chunk-ms", type=int, default=CHUNK_MS_DEFAULT, help="Chunk duration in ms")
    parser.add_argument("--sink-name", default=None, help="PipeWire sink name (auto-detect if omitted)")
    parser.add_argument("--no-wait", action="store_true",
                        help="Upload immediately without waiting for active meeting")
    parser.add_argument("--list-sinks", action="store_true", help="List available sinks and exit")
    args = parser.parse_args()

    # Graceful shutdown on Ctrl-C / SIGTERM
    def _handle_signal(sig, frame):
        log.info("Stopping (signal %d)…", sig)
        _stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Discover sinks
    log.info("Discovering PipeWire audio sinks…")
    nodes = get_pipewire_nodes()
    sinks = find_audio_sinks(nodes)

    if not sinks:
        log.error("No Audio/Sink nodes found in PipeWire. Is PipeWire running?")
        sys.exit(1)

    log.info("Found %d sink(s):", len(sinks))
    for s in sorted(sinks, key=score_sink, reverse=True):
        marker = "← SELECTED" if s == max(sinks, key=score_sink) and not args.sink_name else ""
        log.info("  [score=%+d] %s  (%s) %s",
                 score_sink(s), s["name"], s["description"], marker)

    if args.list_sinks:
        sys.exit(0)

    chosen = choose_sink(sinks, args.sink_name)
    if not chosen:
        log.error("Could not select a sink.")
        sys.exit(1)

    log.info("Selected: %s (%s)", chosen["name"], chosen["description"])
    log.info("Using pw-cat --target %s (monitor loopback)", chosen["pw_target"])

    # Check backend health
    if not check_backend_health(args.backend):
        log.error(
            "Backend at %s is not responding.\n"
            "Start it first:\n"
            "  cd backend && HF_HUB_OFFLINE=1 .venv/bin/python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000",
            args.backend,
        )
        sys.exit(1)

    # Optionally wait for active meeting
    if not args.no_wait:
        if not wait_for_meeting(args.backend):
            log.info("Exiting (no meeting started).")
            sys.exit(0)
    else:
        log.info("--no-wait: uploading chunks immediately (409s expected until Start is clicked).")

    # Run the capture loop
    capture_loop(chosen["pw_target"], args.backend, args.chunk_ms)


if __name__ == "__main__":
    main()
