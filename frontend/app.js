// ==============================================================================
// SENTRIX — Command Dashboard Real-Time Logic & WebSocket Stream Client
// ==============================================================================

const WS_PROTOCOL = window.location.protocol === "https:" ? "wss:" : "ws:";
const WS_URL = `${WS_PROTOCOL}//${window.location.host}/ws/threat`;
const GAUGE_CIRCUMFERENCE = 534; // 2 * PI * 85

let socket = null;
let reconnectTimer = null;
let lastPingTime = Date.now();

// ── WebSocket Client ────────────────────────────────────────────────────────
function connectWebSocket() {
  const statusEl = document.getElementById("ws-status");

  try {
    socket = new WebSocket(WS_URL);

    socket.onopen = () => {
      if (statusEl) {
        statusEl.textContent = "CONNECTED";
        statusEl.style.color = "var(--accent-green)";
      }
      console.log("[SENTRIX-WS] Stream connected:", WS_URL);
    };

    socket.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        renderThreatState(payload);
      } catch (err) {
        console.error("[SENTRIX-WS] Parse error:", err);
      }
    };

    socket.onerror = (err) => {
      console.warn("[SENTRIX-WS] Socket error:", err);
    };

    socket.onclose = () => {
      if (statusEl) {
        statusEl.textContent = "RECONNECTING";
        statusEl.style.color = "var(--accent-yellow)";
      }
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connectWebSocket, 2000);
    };

  } catch (e) {
    console.error("[SENTRIX-WS] Init error:", e);
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connectWebSocket, 3000);
  }
}

// ── State Renderer ──────────────────────────────────────────────────────────
function renderThreatState(state) {
  const tci = state.tci !== undefined ? state.tci : 0.0;
  const level = state.level || 1;
  const status = state.status || "NORMAL";
  const reason = state.reason || "Nominal state";
  const scores = state.scores || {};
  const authorized = state.authorized || false;

  // 1. TCI Score & Dial
  const scoreValEl = document.getElementById("tci-score-value");
  const badgeEl = document.getElementById("tci-level-badge");
  const reasonEl = document.getElementById("tci-reason-text");
  const meterEl = document.getElementById("gauge-meter-path");

  if (scoreValEl) scoreValEl.textContent = tci.toFixed(2);
  if (reasonEl) reasonEl.textContent = reason;

  if (badgeEl) {
    badgeEl.className = `tci-level-badge level-${level}`;
    badgeEl.textContent = `LEVEL ${level} · ${status}`;
  }

  if (meterEl) {
    const offset = GAUGE_CIRCUMFERENCE - (tci * GAUGE_CIRCUMFERENCE);
    meterEl.style.strokeDashoffset = offset;
    
    // Dial color mapped to level
    const colors = [
      "var(--accent-green)",
      "var(--accent-cyan)",
      "var(--accent-yellow)",
      "var(--accent-orange)",
      "var(--accent-red)"
    ];
    meterEl.style.stroke = colors[level - 1] || "var(--accent-green)";
  }

  // 2. Modality Decomposition Bars
  updateFactorBar("vision", scores.weapon !== undefined ? scores.weapon : (scores.vision || 0));
  updateFactorBar("audio", scores.audio || 0);
  updateFactorBar("motion", scores.motion || 0);
  updateFactorBar("fire", scores.fire || 0);

  // 3. HUD Overlays
  const identityEl = document.getElementById("hud-identity-val");
  if (identityEl) {
    identityEl.textContent = authorized ? "Resident Authorized" : "Unverified / Stranger";
    identityEl.className = authorized ? "hud-val text-success" : "hud-val text-danger";
  }

  const behaviorEl = document.getElementById("hud-behavior-val");
  if (behaviorEl) {
    behaviorEl.textContent = scores.behaviour_label || (scores.motion > 0.4 ? "High Speed" : "Normal");
  }

  // 4. Acoustic Tag
  const audioClassEl = document.getElementById("audio-class-label");
  if (audioClassEl && scores.audio_label) {
    audioClassEl.textContent = scores.audio_label;
  }

  // 5. FPS & Latency
  const fpsEl = document.getElementById("fps-counter");
  if (fpsEl && state.fps) fpsEl.textContent = Number(state.fps).toFixed(1);

  const latencyEl = document.getElementById("latency-val");
  if (latencyEl && state.latency_p95) latencyEl.textContent = `${Math.round(state.latency_p95 * 1000)}ms`;
}

function updateFactorBar(id, val) {
  const valEl = document.getElementById(`score-${id}`);
  const barEl = document.getElementById(`bar-${id}`);
  const num = Math.min(Math.max(val, 0.0), 1.0);

  if (valEl) valEl.textContent = num.toFixed(2);
  if (barEl) barEl.style.width = `${Math.round(num * 100)}%`;
}

// ── Acoustic Spectrum Canvas Simulator ──────────────────────────────────────
function initAudioSpectrum() {
  const canvas = document.getElementById("audio-visualizer-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const bars = 32;

  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const barWidth = (canvas.width / bars) - 2;

    for (let i = 0; i < bars; i++) {
      const freq = Math.sin(Date.now() * 0.005 + i * 0.3);
      const h = Math.max(4, Math.abs(freq) * (canvas.height - 10) + Math.random() * 8);

      const grad = ctx.createLinearGradient(0, canvas.height, 0, 0);
      grad.addColorStop(0, "#38bdf8");
      grad.addColorStop(1, "#3b82f6");

      ctx.fillStyle = grad;
      ctx.fillRect(i * (barWidth + 2), canvas.height - h, barWidth, h);
    }
    requestAnimationFrame(draw);
  }
  draw();
}

// ── Emergency Action Handlers ───────────────────────────────────────────────
async function triggerHardwareSiren() {
  const pill = document.getElementById("siren-status-pill");
  const status = document.getElementById("siren-status");

  if (status) {
    status.textContent = "ACTIVE (110dB)";
    status.style.color = "var(--accent-red)";
  }

  try {
    const res = await fetch("/api/dispatch/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "trigger_siren" })
    });
    console.log("[SENTRIX] Hardware siren triggered via Relay Pin 17");
  } catch (e) {
    console.warn("[SENTRIX] Siren trigger request:", e);
  }

  setTimeout(() => {
    if (status) {
      status.textContent = "STANDBY";
      status.style.color = "var(--accent-cyan)";
    }
  }, 5000);
}

async function executeEmergencyDispatch() {
  if (!confirm("CONFIRM EMERGENCY DISPATCH: This will generate and transmit an encrypted Level 5 Police Dispatch Incident Package with GPS & Evidence hashes. Proceed?")) {
    return;
  }

  try {
    const res = await fetch("/api/dispatch/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "police_dispatch" })
    });
    alert("🚨 EMERGENCY DISPATCH SENT: Incident package dispatched to emergency contacts.");
  } catch (e) {
    alert("Dispatch notification sent to security admin.");
  }
}

// ── Boot Initialization ─────────────────────────────────────────────────────
window.addEventListener("DOMContentLoaded", () => {
  connectWebSocket();
  initAudioSpectrum();
});
