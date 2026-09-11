#!/usr/bin/env bash
# start_with_loopback.sh — Start backend + PipeWire loopback capture together.
#
# Usage:
#   cd "/path/to/GMEET RECORDER"
#   bash backend/scripts/start_with_loopback.sh
#
# The backend starts first, then the loopback script waits for you to click
# "Start Transcription" in the extension popup before sending audio.
# The loopback captures whatever your default audio output (speakers, BT
# headphones) plays — including remote participants AND your own voice through
# Meet's monitoring path.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$SCRIPT_DIR")"

# Resolve the Python interpreter: prefer the venv's python3 directly (the pip
# scripts may have stale shebangs after a directory move, but the binary symlink
# always works).
PYTHON_CANDIDATES=(
    "$BACKEND_DIR/.venv/bin/python3"
    "$BACKEND_DIR/.venv/bin/python3.12"
    "$BACKEND_DIR/.venv/bin/python3.11"
    "$BACKEND_DIR/.venv/bin/python3.10"
)
PYTHON=""
for candidate in "${PYTHON_CANDIDATES[@]}"; do
    if [[ -x "$candidate" ]]; then
        PYTHON="$candidate"
        break
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo "ERROR: Python venv not found at $BACKEND_DIR/.venv"
    echo "Run:  cd backend && python3 -m venv .venv && .venv/bin/python3 -m pip install -r requirements.txt"
    exit 1
fi

# Check FFmpeg
if ! command -v ffmpeg &>/dev/null; then
    echo "ERROR: ffmpeg not found. Install:  sudo apt install -y ffmpeg"
    exit 1
fi

# Check pw-cat
if ! command -v pw-cat &>/dev/null; then
    echo "ERROR: pw-cat not found. Install:  sudo apt install -y pipewire-audio-client-libraries"
    exit 1
fi

# Ensure requests is available in the venv
"$PYTHON" -c "import requests" 2>/dev/null || {
    echo "Installing requests in venv…"
    "$PYTHON" -m pip install -q requests
}

echo "=============================================="
echo "  Google Meet Transcriber — Loopback Mode"
echo "=============================================="
echo ""
echo "[1/3] Python : $PYTHON"
echo "[2/3] Backend: $BACKEND_DIR"
echo ""
echo "Starting FastAPI backend on 127.0.0.1:8000…"

# Start backend in background, run from backend dir so relative imports work
(
    cd "$BACKEND_DIR"
    HF_HUB_OFFLINE=1 "$PYTHON" -m uvicorn app.main:app \
        --host 127.0.0.1 --port 8000 \
        --log-level info
) &
BACKEND_PID=$!
echo "Backend PID: $BACKEND_PID"

# Wait for backend to respond
echo "Waiting for backend to be ready…"
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:8000/health &>/dev/null; then
        echo "✓ Backend ready (${i}×0.5s)"
        break
    fi
    sleep 0.5
    if [[ $i -eq 30 ]]; then
        echo "ERROR: Backend did not start in 15s. Check for port 8000 conflicts."
        kill "$BACKEND_PID" 2>/dev/null
        exit 1
    fi
done

echo ""
echo "Starting PipeWire loopback audio capture…"
echo "Auto-detecting your active audio output (speakers / BT headphones)…"
echo ""
echo "  → Click 'Start Transcription' in the Brave extension popup to begin."
echo "  → Press Ctrl+C here to stop everything."
echo ""

# Run loopback capture (blocks until Ctrl-C or capture ends)
"$PYTHON" "$SCRIPT_DIR/pipewire_loopback_capture.py" --backend http://127.0.0.1:8000

EXIT_CODE=$?
echo ""
echo "Stopping backend (PID $BACKEND_PID)…"
kill "$BACKEND_PID" 2>/dev/null || true
wait "$BACKEND_PID" 2>/dev/null || true
echo "Done. Transcripts are in:  $BACKEND_DIR/../meetings/"
exit $EXIT_CODE
