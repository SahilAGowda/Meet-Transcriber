const BACKEND = "http://127.0.0.1:8000";

function log(event, detail = "") {
  console.info("[MeetTranscriber]", detail ? `${event} | ${detail}` : event);
}

async function resetOffscreen() {
  const contexts = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] });
  if (contexts.length) {
    await chrome.runtime.sendMessage({ type: "end-capture" }).catch(() => {});
    await chrome.offscreen.closeDocument();
    log("offscreen_reset", "closed stale offscreen document");
  }
  await chrome.offscreen.createDocument({
    url: "offscreen/offscreen.html",
    reasons: ["USER_MEDIA"],
    justification: "Capture Google Meet tab audio and microphone for local transcription.",
  });
}

async function waitForOffscreen(maxMs = 5000) {
  const deadline = Date.now() + maxMs;
  while (Date.now() < deadline) {
    try {
      const pong = await chrome.runtime.sendMessage({ type: "offscreen-ping" });
      if (pong?.ok) return;
    } catch {
      // Offscreen script still loading.
    }
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error("Capture page failed to load. Reload the extension at brave://extensions");
}

async function backend(path, options = {}) {
  const response = await fetch(`${BACKEND}${path}`, options);
  if (!response.ok) throw new Error((await response.text()) || `Backend error ${response.status}`);
  return response.json();
}

function isMeet(tab) {
  return /^https:\/\/meet\.google\.com\//.test(tab?.url || "");
}

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  (async () => {
    if (message.type === "start") {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      log("start_requested", tab?.url || "no active tab");
      if (!isMeet(tab)) throw new Error("Open the Google Meet tab first, then click Start.");
      await backend("/health");
      const config = await backend("/config");
      const result = await backend("/meeting/start", { method: "POST" });
      log("backend_session_started", result.transcript);
      try {
        await resetOffscreen();
        await waitForOffscreen();
        const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
        log("tab_capture_stream_id", `tabId=${tab.id}`);
        const capture = await chrome.runtime.sendMessage({
          type: "begin-capture",
          streamId,
          timesliceMs: config.audio_chunk_milliseconds,
        });
        if (!capture?.ok) throw new Error(capture?.error || "Audio capture could not start.");
        log("capture_running", `${result.transcript} version=${capture.version || "?"} mode=${capture.mode || "?"}`);
        await chrome.storage.local.set({ active: true, transcript: result.transcript, meetingTabId: tab.id });
        reply({
          ok: true,
          transcript: result.transcript,
          warning: capture.mode === "tab-only"
            ? "Tab-only capture (extension mic blocked). Grant mic at brave://settings/content/microphone for “Google Meet Transcriber”, then restart."
            : undefined,
        });
      } catch (error) {
        log("start_failed", error.message);
        await backend("/meeting/stop", { method: "POST" });
        throw error;
      }
    } else if (message.type === "stop") {
      log("stop_requested");
      await chrome.runtime.sendMessage({ type: "end-capture" }).catch(() => {});
      const result = await backend("/meeting/stop", { method: "POST" });
      await chrome.storage.local.set({ active: false });
      log("stop_complete", result.transcript || "no transcript");
      reply({ ok: true, transcript: result.transcript });
    } else if (message.type === "status") {
      reply({ ok: true, ...(await backend("/meeting/status")) });
    } else if (message.type === "open") {
      chrome.tabs.create({ url: `${BACKEND}/meeting/transcript` });
      reply({ ok: true });
    }
  })().catch(error => reply({ ok: false, error: error.message }));
  return true;
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const state = await chrome.storage.local.get(["active", "meetingTabId"]);
  if (state.active && state.meetingTabId === tabId) {
    log("meet_tab_closed", `tabId=${tabId}`);
    chrome.runtime.sendMessage({ type: "end-capture" }).catch(() => {});
    backend("/meeting/stop", { method: "POST" }).catch(() => {});
    chrome.storage.local.set({ active: false });
  }
});
