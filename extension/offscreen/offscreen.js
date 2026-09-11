let captureLoopActive = false;
let captureHandles;
const uploads = new Set();
const BACKEND = "http://127.0.0.1:8000";
const CAPTURE_VERSION = "0.2.5-mic-retry";

function withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms)),
  ]);
}

function pickMimeType() {
  for (const type of ["audio/webm;codecs=opus", "audio/webm"]) {
    if (MediaRecorder.isTypeSupported(type)) return type;
  }
  return "";
}

function debugLog(event, detail = "") {
  const line = detail ? `${event} | ${detail}` : event;
  console.info("[MeetTranscriber offscreen]", line);
  fetch(`${BACKEND}/meeting/debug/extension`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ event, detail }),
  }).catch(() => {});
}

function uploadWebm(blob, meta) {
  const form = new FormData();
  form.append("chunk", blob, "meet-audio.webm");
  debugLog("chunk_upload_start", `seq=${meta.seq} reason=${meta.reason} uploadBytes=${blob.size}`);
  const upload = fetch(`${BACKEND}/meeting/audio`, { method: "POST", body: form })
    .then(response => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      debugLog("chunk_upload_ok", `seq=${meta.seq}`);
    })
    .catch(error => {
      debugLog("chunk_upload_failed", `seq=${meta.seq} error=${error.message}`);
      console.error("Local audio upload failed", error);
    })
    .finally(() => uploads.delete(upload));
  uploads.add(upload);
}

async function recordOneChunk(stream, mimeType, timesliceMs) {
  const recorder = new MediaRecorder(stream, { mimeType, audioBitsPerSecond: 64000 });
  const parts = [];
  await new Promise((resolve, reject) => {
    recorder.ondataavailable = ({ data }) => {
      if (data.size) parts.push(data);
    };
    recorder.onerror = () => reject(new Error("MediaRecorder failed while recording a chunk"));
    recorder.onstop = resolve;
    recorder.start();
    setTimeout(() => {
      if (recorder.state === "recording") recorder.stop();
    }, timesliceMs);
  });
  return new Blob(parts, { type: mimeType });
}

/**
 * Try to acquire the microphone with multiple strategies.
 *
 * Strategy 1: Standard getUserMedia (may fail if Meet holds the mic).
 * Strategy 2: Enumerate devices and pick a device by deviceId directly —
 *   sometimes works even when the default audio input is claimed, because
 *   Chromium's device arbitration is per-device-path, not per-input-class.
 * Strategy 3: Request with echoCancellation:false — avoids APM pipeline
 *   conflicts that can cause dismissals when another tab holds the default input.
 *
 * Returns a MediaStream on success, null on failure.
 */
async function tryGetMicStream() {
  const strategies = [
    // Strategy 1: standard
    async () => {
      return await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        video: false,
      });
    },
    // Strategy 2: no APM (avoids pipeline conflict with Meet's WebRTC)
    async () => {
      return await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
        video: false,
      });
    },
    // Strategy 3: enumerate devices and pick first audioinput explicitly
    async () => {
      const devices = await navigator.mediaDevices.enumerateDevices();
      const audioInputs = devices.filter(d => d.kind === "audioinput");
      if (!audioInputs.length) throw new Error("no audio input devices found");
      // Try each available device
      for (const device of audioInputs) {
        try {
          const stream = await navigator.mediaDevices.getUserMedia({
            audio: { deviceId: { exact: device.deviceId } },
            video: false,
          });
          return stream;
        } catch {
          // Try next device
        }
      }
      throw new Error("all audio input devices denied");
    },
  ];

  for (let i = 0; i < strategies.length; i++) {
    try {
      debugLog("mic_attempt", `strategy=${i + 1}`);
      const stream = await withTimeout(strategies[i](), 4000, `mic_strategy_${i + 1}`);
      return stream;
    } catch (err) {
      debugLog("mic_attempt_failed", `strategy=${i + 1} error=${err.message}`);
    }
  }
  return null;
}

async function buildCaptureStream(streamId) {
  const tabStream = await navigator.mediaDevices.getUserMedia({
    audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: streamId } },
    video: false,
  });

  const tabTracks = tabStream.getAudioTracks();
  debugLog(
    "tab_stream_acquired",
    tabTracks.map(track => `${track.label || "tab"} enabled=${track.enabled} muted=${track.muted}`).join("; ") || "no tracks",
  );

  // Try mic with multiple strategies
  let micStream = null;
  micStream = await tryGetMicStream();
  if (micStream) {
    debugLog(
      "mic_stream_acquired",
      micStream.getAudioTracks().map(track => `${track.label || "mic"} enabled=${track.enabled} muted=${track.muted}`).join("; "),
    );
  } else {
    debugLog("mic_stream_skipped", "all strategies failed — tab-only mode");
  }

  const audioContext = new AudioContext();
  await audioContext.resume();
  debugLog("audio_context", `state=${audioContext.state} sampleRate=${audioContext.sampleRate}`);

  const recordDestination = audioContext.createMediaStreamDestination();
  const tabSource = audioContext.createMediaStreamSource(tabStream);
  tabSource.connect(recordDestination);
  // Keep tab audio playing through speakers so the meeting remains audible
  tabSource.connect(audioContext.destination);

  if (micStream) {
    audioContext.createMediaStreamSource(micStream).connect(recordDestination);
  }

  return {
    audioContext,
    tabStream,
    micStream,
    recordStream: recordDestination.stream,
    captureMode: micStream ? "tab+mic" : "tab-only",
  };
}

function stopTracks(stream) {
  stream?.getTracks().forEach(track => track.stop());
}

function cleanupCapture() {
  stopTracks(captureHandles?.tabStream);
  stopTracks(captureHandles?.micStream);
  captureHandles?.audioContext?.close();
  captureHandles = null;
}

async function runCaptureLoop(timesliceMs) {
  const mimeType = pickMimeType();
  if (!mimeType) throw new Error("This browser cannot record WebM audio from the Meet tab.");

  debugLog("mediarecorder_config", `mime=${mimeType} timesliceMs=${timesliceMs || 1500} mode=complete_webm_per_chunk`);
  let seq = 0;

  while (captureLoopActive) {
    const blob = await recordOneChunk(captureHandles.recordStream, mimeType, timesliceMs || 1500);
    if (!captureLoopActive) break;
    if (!blob.size) {
      debugLog("chunk_skipped", "empty_blob");
      continue;
    }
    seq += 1;
    debugLog("chunk_prepared", `seq=${seq} reason=complete_webm raw=${blob.size} upload=${blob.size}`);
    uploadWebm(blob, { seq, reason: "complete_webm" });
  }

  debugLog("capture_stopped", `uploadsPending=${uploads.size} chunks=${seq}`);
  cleanupCapture();
}

async function startCapture(streamId, timesliceMs) {
  if (captureLoopActive) {
    debugLog("capture_already_running", CAPTURE_VERSION);
    return;
  }

  debugLog("capture_version", CAPTURE_VERSION);
  captureHandles = await buildCaptureStream(streamId);
  debugLog("capture_mode", captureHandles.captureMode);

  captureLoopActive = true;
  debugLog("capture_started", `mode=${captureHandles.captureMode}`);
  runCaptureLoop(timesliceMs).catch(error => {
    debugLog("capture_failed", error.message);
    captureLoopActive = false;
    cleanupCapture();
  });
}

async function stopCapture() {
  captureLoopActive = false;
  await Promise.all([...uploads]);
}

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (message.type === "offscreen-ping") {
    reply({ ok: true, version: CAPTURE_VERSION });
    return;
  }
  if (message.type === "begin-capture") {
    startCapture(message.streamId, message.timesliceMs)
      .then(() => reply({
        ok: true,
        version: CAPTURE_VERSION,
        mode: captureHandles?.captureMode || "unknown",
      }))
      .catch(error => {
        debugLog("capture_failed", error.message);
        reply({ ok: false, error: error.message });
      });
    return true;
  }
  if (message.type === "end-capture") {
    stopCapture().then(() => reply({ ok: true }));
    return true;
  }
});
