#!/usr/bin/env bash
# Automated integration test: backend API + WebM chunks + Whisper pipeline
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/backend/.venv/bin/python3"
BACKEND="http://127.0.0.1:8000"
TMP="$ROOT/tests/.tmp"
export HF_HUB_OFFLINE=1

mkdir -p "$TMP"

echo "=== 1. Unit tests ==="
PYTHONPATH="$ROOT/backend" "$VENV" -c "
from app.service import deduplicate
from app.transcript import TranscriptWriter
from datetime import datetime
from pathlib import Path
assert deduplicate(\"Let's discuss the database\", 'database schema first') == 'schema first'
p = Path('$TMP/unit_test.md')
w = TranscriptWriter(p, datetime(2026, 9, 1, 16, 30, 21))
w.append(3, 'Speaker 1', 'Good morning')
assert '[00:03] Speaker 1:\nGood morning' in p.read_text()
print('PASS: unit tests')
"

echo "=== 2. Health check ==="
curl -sf "$BACKEND/health" | tee "$TMP/health.json"
echo ""

echo "=== 3. Config ==="
curl -sf "$BACKEND/config" | tee "$TMP/config.json"
echo ""

echo "=== 4. Generate test speech (espeak or ffmpeg fallback) ==="
if command -v espeak-ng >/dev/null 2>&1; then
  espeak-ng -w "$TMP/speech1.wav" -s 150 "Good morning everyone. Welcome to the meeting."
  espeak-ng -w "$TMP/speech2.wav" -s 140 -p 50 "Thank you. Let us start with the database discussion."
elif command -v espeak >/dev/null 2>&1; then
  espeak -w "$TMP/speech1.wav" -s 150 "Good morning everyone. Welcome to the meeting."
  espeak -w "$TMP/speech2.wav" -s 140 -p 50 "Thank you. Let us start with the database discussion."
else
  echo "No espeak; using ffmpeg sine tone (Whisper may produce little text)"
  ffmpeg -y -hide_banner -loglevel error -f lavfi -i "sine=frequency=440:duration=2" -ar 16000 -ac 1 "$TMP/speech1.wav"
  ffmpeg -y -hide_banner -loglevel error -f lavfi -i "sine=frequency=880:duration=2" -ar 16000 -ac 1 "$TMP/speech2.wav"
fi

# Split into ~1.5s chunks and encode as WebM/Opus (matches MediaRecorder)
for i in 1 2; do
  ffmpeg -y -hide_banner -loglevel error -i "$TMP/speech${i}.wav" -ar 48000 -ac 1 "$TMP/speech${i}_48k.wav"
done

echo "=== 5. Start meeting ==="
curl -sf -X POST "$BACKEND/meeting/start" | tee "$TMP/start.json"
echo ""

echo "=== 6. Upload WebM chunks ==="
CHUNK_SEC=1.5
for wav in "$TMP"/speech*_48k.wav; do
  base=$(basename "$wav" .wav)
  duration=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$wav")
  offset=0
  n=0
  while awk "BEGIN {exit !($offset < $duration)}"; do
    out="$TMP/${base}_chunk${n}.webm"
    ffmpeg -y -hide_banner -loglevel error -ss "$offset" -t "$CHUNK_SEC" -i "$wav" \
      -c:a libopus -b:a 64k -ar 48000 "$out" 2>/dev/null || break
    if [[ ! -s "$out" ]]; then break; fi
    echo "  Uploading $(basename "$out") ($(wc -c < "$out") bytes)"
    curl -sf -X POST "$BACKEND/meeting/audio" -F "chunk=@$out;type=audio/webm" >/dev/null
    offset=$(awk "BEGIN {print $offset + $CHUNK_SEC}")
    n=$((n + 1))
  done
done

echo "=== 7. Status + metrics ==="
curl -sf "$BACKEND/meeting/status" | tee "$TMP/status.json"
echo ""

echo "=== 8. Transcript path ==="
curl -sf "$BACKEND/meeting/transcript/path" | tee "$TMP/path.json"
echo ""

echo "=== 9. Stop meeting ==="
curl -sf -X POST "$BACKEND/meeting/stop" | tee "$TMP/stop.json"
echo ""

echo "=== 10. Transcript content ==="
TRANSCRIPT=$(python3 -c "import json; print(json.load(open('$TMP/path.json'))['path'])")
echo "File: $TRANSCRIPT"
echo "---"
cat "$TRANSCRIPT"
echo "---"

echo "=== 11. Whisper offline benchmark (base.en, 1.5s real speech chunk) ==="
ffmpeg -y -hide_banner -loglevel error -ss 0 -t 1.5 -i "$TMP/speech1_48k.wav" -ar 16000 -ac 1 "$TMP/bench.wav"
PYTHONPATH="$ROOT/backend" "$VENV" -c "
import time, json
from app.audio import read_wav
from app.service import Transcriber
audio = read_wav('$TMP/bench.wav')
t = time.time()
text = Transcriber().transcribe(audio)
elapsed = time.time() - t
print(f'Whisper base.en 1.5s speech: {elapsed:.3f}s')
print(f'Text: {text!r}')
print(f'Estimated live latency (chunk + inference): {1.5 + elapsed:.3f}s')
"

echo ""
echo "=== DONE ==="
