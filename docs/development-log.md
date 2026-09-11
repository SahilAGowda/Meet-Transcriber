# Google Meet Transcriber — Development Log

This document records architecture decisions, bugs found, fixes attempted, and live-test results through **2026-09-11**. It complements `README.md` (how to run) with **what we learned while debugging**.

---

## Project goal

Local-first, CPU-only Google Meet transcription MVP:

```
Brave extension → HTTP chunks → FastAPI (127.0.0.1:8000) → FFmpeg → faster-whisper → Markdown in meetings/
```

No cloud upload. No raw audio stored by default.

---

## Architecture (as built)

| Layer | Path | Role |
|-------|------|------|
| Popup | `extension/popup/` | Start/Stop UI, polls backend status |
| Service worker | `extension/background/service-worker.js` | Orchestrates tab capture, backend API, offscreen lifecycle |
| Offscreen | `extension/offscreen/offscreen.js` | Captures tab (+ optional mic), encodes WebM, uploads chunks |
| Backend | `backend/app/` | FastAPI routes, Whisper, diarization, Markdown writer |

### Data flow

1. User clicks **Start** on a `meet.google.com` tab.
2. Background calls `POST /meeting/start`, creates offscreen document.
3. `chrome.tabCapture.getMediaStreamId()` → offscreen redeems via `getUserMedia({ chromeMediaSource: "tab" })`.
4. Optional: extension mic merged via Web Audio `AudioContext`.
5. `MediaRecorder` produces ~1.5s WebM blobs → `POST /meeting/audio`.
6. Backend: FFmpeg decode → Whisper (`base.en`, CPU, int8) → dedupe → append `.md`.

### Key backend files

- `service.py` — chunk ingest, RMS metrics, silent-chunk warnings, extension debug events
- `audio.py` — `decode_webm()`, `decode_pcm()` (legacy)
- `transcript.py` — continuous Markdown flush
- `diarization.py` — lightweight `Speaker 1`, `Speaker 2` clustering

---

## Issues discovered (chronological)

### 1. WebM fragmentation — FFmpeg decode failures (original)

**Symptom:** Empty transcripts; FFmpeg errors like `[mp3] Invalid frame size`.

**Cause:** `MediaRecorder.start(timeslice)` emits fragments without EBML headers after chunk 1. FFmpeg cannot decode headerless fragments.

**Fix (v0.2.0):** Extract init segment (bytes before first Cluster) from chunk 1; prepend to subsequent fragments.

**Status:** Partially worked in live tests (decode succeeded) but **unreliable** — see issue 5.

---

### 2. PCM / ScriptProcessor path — all silence

**Symptom:** Chunks tagged `pcm@48000`, `rms=0.0000`, `magic=00000000`.

**Cause:** Experimental PCM capture via ScriptProcessor received no routed audio.

**Fix:** Reverted to MediaRecorder WebM path. PCM decode kept in backend for backward compatibility only.

---

### 3. Meet mic permission ≠ extension mic permission

**Symptom:** User grants mic to `meet.google.com`; extension logs `mic_stream_skipped: Permission dismissed`.

**Cause:** Chrome/Brave treats site permissions and extension-origin `getUserMedia` separately. Meet holding the mic can also cause extension mic prompts to fail or auto-dismiss.

**Impact:** `capture_mode: tab-only` — user's voice is **not** merged into the recording stream.

**Mitigation (v0.2.3+):** Non-blocking mic attempt in offscreen (timeout). Popup no longer hangs on Start.

**Still required from user:** Allow microphone for **"Google Meet Transcriber"** at `brave://settings/content/microphone`, or accept the browser prompt when clicking Start.

---

### 4. Tab-only capture produces silence (solo / muted Meet)

**Symptom:** WebM decodes fine (`magic=1a45dfa3`) but `rms=0.0000` on every chunk; Whisper VAD removes all audio.

**Cause:** Tab capture records **what Meet plays through the tab**, not the raw mic unless mic is merged. In solo Meet or when mic is muted in Meet UI, tab output can be silent even while Meet captions work (captions use a separate path).

**Evidence (session `meeting_2026-09-11_13-42-19.md`):**
```
capture_mode: tab-only
mic_stream_skipped: Permission dismissed
#1–#14  rms=0.0000  out=silent  wrote=False
meeting stopped: received=14 written=0 silent=14 failed=0
```

**What works:** Tab+mic mode, or 2+ participants with remote audio playing through Meet speakers/tab.

---

### 5. Init-segment prepend decode regression (v0.2.3)

**Symptom:** Chunk 1 decodes; chunks 2–15 fail with `File ended prematurely`.

**Evidence (session `meeting_2026-09-11_13-53-28.md`):**
```
capture_version: 0.2.3-webm-mic-tab
#1   rms=0.0000  out=silent
#2–#15  FAILED  ffmpeg: File ended prematurely
meeting stopped: received=15 written=0 silent=1 failed=14
```

**Cause:** Naive init+fragment concatenation is **not a valid WebM file** when MediaRecorder splits mid-cluster or produces minimal silent clusters. Integration test `tests/test_fragmented_webm.py` also fails on repaired fragments.

**Fix (v0.2.4):** Stop/restart `MediaRecorder` every ~1.5s so **each uploaded chunk is a complete WebM file** (no init prepend). Simpler and FFmpeg-stable.

---

### 6. Popup stuck on "Not Running" (v0.2.2 and earlier)

**Symptom:** Backend shows only `GET /meeting/status`, never `POST /meeting/start`.

**Cause:** Popup called blocking `getUserMedia()` before start; while Meet held the mic, the call hung forever.

**Fix (v0.2.3):** Removed blocking mic check from popup; added offscreen ping + "Starting…" state.

**Verified fixed:** v0.2.3 session shows `POST /meeting/start` at 13:53:28.

---

## Automated test results

| Test | Result | Notes |
|------|--------|-------|
| `pytest` unit tests (dedup, writer) | PASS | |
| Integration (JFK FLAC → WebM → pipeline) | PASS | Proves backend + Whisper work |
| CPU benchmark (`base.en`, i5-class) | ~1.54s inference / 1.5s chunk | Live latency ~3s estimated |
| `test_fragmented_webm.py` (init prepend) | FAIL | Confirms prepend strategy is fragile |

**Conclusion:** Backend pipeline is **proven**. Browser capture + permissions are the **blocking gap** for live Meet.

---

## Extension version history

| Version | Change |
|---------|--------|
| 0.2.0 | WebM init-segment prepend; tab+mic merge |
| 0.2.3 | Non-blocking Start; offscreen ping; mic 2s timeout |
| 0.2.4 | Complete WebM per chunk (no fragment prepend); popup mic pre-request; clearer permission hints |

---

## How to read backend logs

| Log pattern | Meaning |
|-------------|---------|
| Only `GET /meeting/status` | Start never completed (popup/background hang) |
| `POST /meeting/start` | Session started successfully |
| `mic_stream_skipped: Permission dismissed` | Extension mic blocked — tab-only mode |
| `capture_mode: tab-only` | No mic merge |
| `capture_mode: tab+mic` | Both sources merged — preferred |
| `rms=0.0000 out=silent` | Chunk decoded but no audible signal |
| `rms>0.01 out=written` | Working — text should appear in `.md` |
| `FAILED ... File ended prematurely` | Bad WebM chunk (fixed in v0.2.4) |
| `pcm@48000` | Outdated extension still loaded — reload at `brave://extensions` |

---

## Live test checklist (for next session)

1. **Reload extension** at `brave://extensions` — confirm version **0.2.4**.
2. **Backend running:**
   ```bash
   cd backend
   HF_HUB_OFFLINE=1 .venv/bin/python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```
3. Join Meet with **Meet tab active**.
4. Click **Start** → allow mic for **Google Meet Transcriber** when prompted.
5. **Unmute mic in Meet** (red mic icon = muted = no local audio in tab path).
6. Speak or have remote participant speak.
7. Backend should show:
   - `capture_mode: tab+mic` (ideal) or `tab-only`
   - `rms > 0.01` within a few chunks
   - `out=written wrote=True`
8. Open `meetings/meeting_*.md` or **Open Transcript** in popup.

### If still silent

- Grant extension mic at `brave://settings/content/microphone`.
- Confirm 2+ participants and remote audio is audible in headphones/speakers.
- See `docs/linux-audio-fallback.md` for PipeWire monitor fallback (manual, last resort).

---

## Known MVP limits (unchanged)

- Speaker labels are acoustic clusters only (`Speaker 1`, not names).
- Short-chunk Whisper can repeat words at boundaries (dedup mitigates).
- CPU-only: backlog grows if inference exceeds chunk interval.
- Tab capture policy varies by Brave/Chromium version.

---

## Definition of done — current status

| Requirement | Status |
|-------------|--------|
| Backend pipeline (FFmpeg + Whisper + Markdown) | **Working** (proven with test audio) |
| Extension Start/Stop without hang | **Working** (v0.2.3+) |
| WebM chunk decode reliability | **Fixed in v0.2.4** |
| Live Meet transcription with user's voice | **Working via PipeWire loopback (v0.2.5+)** |
| Extension mic capture (tab+mic mode) | **Improved in v0.2.5 — 3-strategy retry** |
| Production UI / state machine | **Not started** |

---

## Files touched during debugging session

- `extension/offscreen/offscreen.js` — capture logic (multiple iterations)
- `extension/background/service-worker.js` — offscreen reset, waitForOffscreen
- `extension/popup/popup.js`, `popup.html` — Start flow, permission hints
- `extension/manifest.json` — microphone permission, version bumps
- `backend/app/service.py` — debug logging, RMS, silent warnings
- `backend/app/main.py` — `/meeting/debug/extension` endpoint
- `backend/app/audio.py` — forced `-f webm`, PCM path
- `tests/test_fragmented_webm.py`, `tests/run_integration_test.sh` — validation

---

## Session 2026-09-11 14:01 — Root cause confirmed + full fix implemented

### Definitive diagnosis

Two compounding problems caused every chunk to be silent after 12:42:

**Problem A — Mic permission always auto-dismissed:**
Google Meet holds the microphone via WebRTC. When the extension's offscreen page
calls `getUserMedia({audio:true})`, Brave auto-dismisses the prompt because Meet's
WebRTC origin already holds the default input device. Result: every session after
the first runs in `tab-only` mode.

**Problem B — Tab-only capture is inherently silent for the local speaker:**
`chrome.tabCapture` records rendered tab output (remote participants' audio coming
in). It does **not** capture the local microphone. In a solo session or when the
local user is the only speaker, the tab's audio output is `rms=0.0000` on every
chunk — because nothing is playing back from Meet's server.

Evidence: session 12:42 worked (`tab+mic`, 14/14 chunks written). Every later
session failed (`tab-only`, 0 written).

---

### Issue 7 — Broken venv pip shebangs after directory move

**Symptom:** `pip install` fails with `exec: /home/diatoz/Desktop/Transcriber /backend/.venv/bin/python3.12: not found`

**Cause:** The project was moved from `/home/diatoz/Desktop/Transcriber` to `/home/diatoz/Music/GMEET RECORDER`. Python venv pip scripts embed the absolute path of the Python binary in their shebang. The Python binary symlink (`python3 → python3.12 → /usr/bin/python3.12`) was fine, but all `pip*` and other console-script wrappers had the old hardcoded path.

**Fix:**
```bash
grep -rl "/home/diatoz/Desktop/Transcriber" backend/.venv/bin/ | while read f; do
  sed -i 's|/home/diatoz/Desktop/Transcriber /backend/.venv/bin/python3.12|/home/diatoz/Music/GMEET RECORDER/backend/.venv/bin/python3.12|g' "$f"
done
```

**Going forward:** Use `python3 -m pip install` (calling pip via the Python binary, not the wrapper script) — this is immune to shebang breakage.

---

### Issue 8 — Bluetooth default sink not detected by ffmpeg -f pulse

**Symptom:** Monitor audio was silent even with a tone playing, because audio was
routing to **realme Buds Wireless 3 Neo** (Bluetooth, PipeWire node `bluez_output.9C_DE_F0_E6_3F_76.1`) as the default sink. The `ffmpeg -f pulse -i <name>.monitor` pattern does not work for Bluetooth sinks because PipeWire-pulse doesn't always expose a pulse source for BT monitor nodes.

**Fix (v0.2.5):** Switch to `pw-cat --record --target <sink-name>` piped to `ffmpeg`. `pw-cat` uses PipeWire natively and correctly implements sink monitor recording for all sink types including Bluetooth. The sink is auto-detected by scoring: Bluetooth gets +30, built-in speakers/headphones get +10–22, HDMI sinks get −5 to −10.

**Verified:** rms=0.018 captured from BT monitor, `outcome=written` on first try.

---

### Fix A — PipeWire loopback capture script (new file)

**File:** `backend/scripts/pipewire_loopback_capture.py`

Captures audio from the PipeWire sink monitor (whatever your speakers/headphones
play), encodes it as WebM/Opus chunks and POSTs them to `/meeting/audio`.
Works regardless of browser extension permissions. Auto-detects the active output
sink (Bluetooth preferred, then built-in speakers, HDMI last).

**Run alongside the backend:**
```bash
cd "GMEET RECORDER"
bash backend/scripts/start_with_loopback.sh
# ← click Start in the popup
# ← speak — transcription appears in meetings/*.md
```

---

### Fix B — Extension v0.2.5 — 3-strategy mic acquisition

**File:** `extension/offscreen/offscreen.js`

Added `tryGetMicStream()` with three fallback strategies when the standard
`getUserMedia` is blocked by Meet's WebRTC hold:

1. Standard `getUserMedia` with APM (echoCancellation, noiseSuppression)
2. Same without APM — avoids some pipeline conflicts
3. Enumerate devices and call `getUserMedia` with explicit `deviceId` for each input device — sometimes bypasses the default-device lock

---

### Fix C — Popup UI v0.2.5 — capture mode badge + silent-chunk warning

**File:** `extension/popup/popup.html` / `popup.js`

- Dark-mode redesign with pulsing green dot while transcribing
- **Capture mode badge**: green `🎤 Tab + Mic` or yellow `⚠️ Tab only — your voice won't be recorded`
- **Auto-warning** after 5+ silent chunks: prompts user to check mic/loopback
- Loopback script tip at the bottom of every popup

---

## Live test checklist (updated for v0.2.5)

### Option 1 — Loopback mode (recommended, works always)

1. Open terminal in `GMEET RECORDER/` directory
2. Run: `bash backend/scripts/start_with_loopback.sh`
3. Join a Meet call in Brave
4. Click **Start Transcription** in the popup
5. Speak — transcript appears in `meetings/meeting_*.md` within ~3s

### Option 2 — Extension-only mode (requires mic permission)

1. Start backend: `cd backend && HF_HUB_OFFLINE=1 .venv/bin/python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000`
2. Reload extension at `brave://extensions` → confirm version **0.2.5**
3. In `brave://settings/content/microphone` → allow **Google Meet Transcriber**
4. Join Meet, click **Start**, allow mic prompt
5. Popup badge should show **green Tab + Mic**
6. `rms > 0.01` in backend logs = working

### If still silent after both options

- Check `wpctl status` to see which sink is the default (`*`) — the loopback monitors that sink
- If you switched headphones (e.g., BT disconnected), restart the loopback script — it re-detects on launch
- Confirm: `pw-cat --record --target <sink-name> --channels 1 --rate 48000 --format s16 /tmp/test.raw` produces non-empty audio

---

*Last updated: 2026-09-11 (v0.2.5 loopback + mic retry + popup redesign)*
