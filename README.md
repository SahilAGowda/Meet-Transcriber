# Local Google Meet Transcriber for Brave

This is a local-first, CPU-only MVP that captures a **user-selected Google Meet tab** in Brave, sends short audio chunks to `127.0.0.1:8000`, and continuously writes a Markdown transcript to `meetings/`. It neither uploads audio nor stores raw audio by default.

## Capture approach

The extension uses Chromium/Brave's `chrome.tabCapture.getMediaStreamId()` after the user clicks **Start Transcription**. The offscreen extension page redeems that ID via `getUserMedia` with `chromeMediaSource: "tab"`. This is tab *rendered audio*, so it includes the remote people heard from the Google Meet tab—not merely the local microphone. This is the preferred Linux/Brave approach because audio remains inside the browser and needs no system mixer routing.

Chromium's tab-capture documentation also notes that obtaining a tab stream suppresses normal local playback unless the stream is connected to an audio destination; the offscreen recorder deliberately reconnects it so the meeting remains audible. [Chrome tabCapture reference](https://developer.chrome.com/docs/extensions/reference/api/tabCapture)

It requires a recent Brave version built on Chromium 116+; the extension service worker passes the stream ID to an offscreen document, a supported Chromium flow. If this capability is unavailable in a particular Brave release or policy, the extension shows the capture failure rather than silently recording the microphone. A PipeWire/PulseAudio monitor-source fallback is deliberately not enabled automatically because it can capture unrelated system audio and requires user-specific device routing. See `docs/linux-audio-fallback.md` for the manual fallback design.

## Prerequisites

Ubuntu packages:

```bash
sudo apt update
sudo apt install -y python3-venv ffmpeg
```

Set up the CPU backend:

```bash
cd backend
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`faster-whisper` downloads the selected `base.en` model on its first transcription. It runs with `device="cpu"` and `compute_type="int8"`; no CUDA or NVIDIA driver is used. For non-English meetings, change `WHISPER_MODEL` and remove the forced `language="en"` in `backend/app/service.py`.

## Install the Brave extension

1. Open `brave://extensions`.
2. Enable **Developer mode**.
3. Choose **Load unpacked** and select this repository's `extension/` directory.
4. Join a `https://meet.google.com/...` meeting, open the extension, and click **Start Transcription**.
5. Let at least two remote participants speak. The popup displays the live filename; **Open Transcript** serves it from the local backend.
6. Click **Stop Transcription** when finished.

The extension never records automatically and rejects Start outside a Google Meet URL. A closed Meet tab is stopped gracefully.

## Output and latency

Each run creates `meetings/meeting_YYYY-MM-DD_HH-MM-SS.md`; every appended segment is flushed immediately. The default MediaRecorder interval is 1.5 seconds and can be adjusted alongside `AUDIO_CHUNK_SECONDS`.

The backend logs `audio_received_at`, `transcription_started_at`, `transcription_completed_at`, and `transcript_written_at` in `GET /meeting/status` (`metrics`) and in server logs. End-to-end processing latency (`written - received`) excludes the chunk-fill delay; practical speech-to-visible latency is approximately one chunk interval plus this number. Measure it by reading the final spoken word time from a clock and comparing it with the transcript write time. A 2–3 second target is plausible with `tiny.en` or `base.en` on a modern CPU, but cannot be guaranteed: model download, CPU speed, and Whisper inference are the bottlenecks. Do not claim the target until it is measured on the intended machine.

## Model trade-off / benchmark

Start with `base.en` (better recognition, higher CPU cost). If measured latency exceeds the target, set `WHISPER_MODEL=tiny.en`; it is usually faster but less accurate. `small.en` is generally too slow for continuous CPU-only 1.5-second chunks. Benchmark your hardware with a representative mono WAV before joining a meeting:

```bash
cd backend
. .venv/bin/activate
time python scripts/transcribe_wav.py /path/to/sample.wav --chunk-seconds 1.5
```

Record wall time, CPU/memory (`/usr/bin/time -v ...`), and resulting transcript quality for `tiny.en`, `base.en`, and optionally `small.en`. Select the smallest model that reliably produces a processing time below about 1.5 seconds per 1.5-second chunk; otherwise backlog grows. This repository cannot provide a truthful hardware benchmark without your target CPU/audio sample.

## Diarization and known MVP limits

`LocalSpeakerTracker` performs online CPU acoustic clustering and yields `Speaker 1`, `Speaker 2`, etc.; it never attempts identity/name recognition. It is intentionally lightweight to preserve latency and privacy. It can confuse similar voices, speakers with changing microphones, or overlapping speech. For production-grade separation, replace this module with a locally run speaker-embedding/VAD model and benchmark it separately.

Short independent Whisper chunks can repeat words at boundaries, so the backend removes adjacent word overlap. Very short fragments may remain imperfect. Failed chunks are logged and skipped without stopping the meeting.

## Test prerecorded audio path

The script above validates WAV → short chunks → Whisper → local diarization → Markdown before browser integration. Basic non-model checks:

```bash
PYTHONPATH=backend pytest -q
```

## API

- `GET /health`, `GET /config`
- `POST /meeting/start`
- `POST /meeting/audio` (`multipart` field `chunk`)
- `POST /meeting/stop`
- `GET /meeting/status`
- `GET /meeting/transcript` and `/meeting/transcript/path`

The server only listens on `127.0.0.1` in the documented launch command.
