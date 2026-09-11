const el = (id) => document.getElementById(id);
const send = (type, extra = {}) => chrome.runtime.sendMessage({ type, ...extra });
const duration = (seconds) => new Date(seconds * 1000).toISOString().slice(11, 19);

async function ensureExtensionMic() {
  try {
    const stream = await Promise.race([
      navigator.mediaDevices.getUserMedia({ audio: true, video: false }),
      new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), 4000)),
    ]);
    stream.getTracks().forEach(track => track.stop());
    return true;
  } catch {
    return false;
  }
}

function setError(message, type = "error") {
  const box = el("error-box");
  if (!message) {
    box.className = "";
    box.textContent = "";
    return;
  }
  box.className = type;
  box.textContent = message;
}

function setMode(mode) {
  const badge = el("mode-badge");
  const label = el("mode-label");
  const icon  = el("mode-icon");
  if (!mode || mode === "unknown") {
    badge.className = "mode-badge";
    return;
  }
  if (mode === "tab+mic") {
    badge.className = "mode-badge tab-mic";
    icon.textContent = "🎤";
    label.textContent = "Tab + Mic";
  } else {
    badge.className = "mode-badge tab-only";
    icon.textContent = "⚠️";
    label.textContent = "Tab only — your voice won't be recorded";
  }
}

async function refresh() {
  let result;
  try {
    result = await send("status");
  } catch {
    el("status-text").textContent = "Backend unavailable";
    el("dot").className = "dot error";
    el("start").hidden = false;
    el("stop").hidden = true;
    return;
  }

  if (!result.ok) {
    el("status-text").textContent = "Backend unavailable";
    el("dot").className = "dot error";
    el("start").hidden = false;
    el("stop").hidden = true;
    return;
  }

  const active = result.status === "transcribing";
  el("status-text").textContent = active ? "Transcribing…" : "Ready";
  el("dot").className = active ? "dot active" : "dot";
  el("duration-text").textContent = active ? duration(result.duration_seconds) : "";
  el("transcript-text").textContent = result.transcript ? `📝 ${result.transcript}` : "";
  el("start").hidden = active;
  el("stop").hidden = !active;
  el("open").hidden = !result.transcript;

  // Show capture mode badge when active
  if (active && result.debug) {
    // Pull capture_mode from last extension event
    const events = result.debug.extension_events || [];
    const modeEvent = [...events].reverse().find(e => e.startsWith("capture_mode:"));
    if (modeEvent) {
      const mode = modeEvent.replace("capture_mode: ", "").trim();
      setMode(mode);
    }
  } else if (!active) {
    setMode(null);
  }

  // Show silent-chunk warning if transcribing but no output
  if (active) {
    const debug = result.debug || {};
    const totalReceived = debug.chunks_received || 0;
    const totalSilent = debug.chunks_silent || 0;
    const totalWritten = debug.chunks_written || 0;
    if (totalReceived > 5 && totalSilent === totalReceived && totalWritten === 0) {
      setError(
        "All audio chunks are silent — your voice is not reaching the recorder. " +
        "Try: (1) Allow mic for this extension, (2) Unmute in Meet, " +
        "(3) Run the loopback script (see hint below).",
        "warning",
      );
    }
  }
}

el("start").onclick = async () => {
  setError("");
  el("start").disabled = true;
  el("status-text").textContent = "Requesting mic…";
  try {
    const micOk = await ensureExtensionMic();
    if (!micOk) {
      setError(
        "Extension mic not granted — recording tab audio only. " +
        "To include your voice: allow mic for \"Google Meet Transcriber\" at brave://settings/content/microphone.",
        "warning",
      );
    }
    el("status-text").textContent = "Starting…";
    const result = await send("start");
    if (!result.ok) {
      setError(result.error || "Start failed");
    } else if (result.warning) {
      setError(result.warning, "warning");
    }
  } catch (error) {
    setError(error.message || "Start failed");
  } finally {
    el("start").disabled = false;
    await refresh();
  }
};

el("stop").onclick = async () => {
  el("start").disabled = true;
  el("status-text").textContent = "Stopping…";
  setError("");
  setMode(null);
  try {
    await send("stop");
  } finally {
    el("start").disabled = false;
    await refresh();
  }
};

el("open").onclick = () => send("open");
refresh();
setInterval(refresh, 2000);
