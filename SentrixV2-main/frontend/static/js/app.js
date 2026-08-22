// static/js/app.js
// ARCHITECTURE: Connects to /ws/threat WebSocket to receive live state updates.
// Reactively updates the dashboard UI (gauge, scores, dispatch panel, log).

const LEVEL_CONFIG = {
    1: { label: "Level 1 – Normal",     color: "#22c55e", bg: "rgba(34, 197, 94, 0.1)" },
    2: { label: "Level 2 – Suspicious", color: "#facc15", bg: "rgba(250, 204, 21, 0.1)" },
    3: { label: "Level 3 – Elevated",   color: "#f97316", bg: "rgba(249, 115, 22, 0.1)" },
    4: { label: "Level 4 – High",       color: "#ef4444", bg: "rgba(239, 68, 68, 0.1)" },
    5: { label: "Level 5 – Critical",   color: "#dc2626", bg: "rgba(220, 38, 38, 0.1)" },
};

const SCORE_KEYS = ["vision", "audio", "motion", "behaviour", "identity", "weapon", "fire", "theft", "harmful"];

document.addEventListener("DOMContentLoaded", () => {
    
    // Only init full dashboard logic if we are on the dashboard
    const isDashboard = !!document.getElementById("gauge-svg");
    
    // FIX 11: Singleton WebSocket — only ONE connection at any time.
    // Use exponential backoff reconnect instead of page reload to prevent
    // the repeated connect/disconnect/reload spam seen in server logs.
    let ws = null;
    let wsRetryDelay = 1000;  // start at 1s
    let lastLevel = null;
    
    function connectWS() {
        // Prevent duplicate connections
        if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
            return;
        }

        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const wsUrl = `${protocol}//${window.location.host}/ws/threat`;
        ws = new WebSocket(wsUrl);

        ws.onopen = () => {
            wsRetryDelay = 1000; // Reset backoff on successful connection
        };

        ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                updateHealthStatus(data);
                if (isDashboard) {
                    updateDashboard(data, lastLevel);
                    lastLevel = data.level;
                }
            } catch (e) {
                console.error("WS Parse error", e);
            }
        };

        ws.onclose = () => {
            // Exponential backoff: 1s → 2s → 4s → max 30s
            console.log(`WebSocket closed. Reconnecting in ${wsRetryDelay / 1000}s...`);
            setTimeout(() => {
                wsRetryDelay = Math.min(wsRetryDelay * 2, 30000);
                connectWS();
            }, wsRetryDelay);
        };

        ws.onerror = () => {
            // onclose will fire after onerror, so just let it handle reconnect
            ws.close();
        };
    }

    connectWS();
    
    // Fetch initial health status (for pages other than dashboard to set dots quickly)
    fetch('/api/health')
        .then(r => r.json())
        .then(data => {
            if (document.getElementById("dot-cam")) {
                document.getElementById("dot-cam").className = "status-dot " + (data.camera_available ? "online" : "");
                document.getElementById("dot-mic").className = "status-dot " + (data.mic_available ? "online" : "");
                document.getElementById("dot-cloud").className = "status-dot " + (data.cloud_available ? "online" : "");
            }
        }).catch(() => {});
});



// --- Dashboard Updaters ---

function updateDashboard(data, lastLevel) {
    updateGauge(data.tci);
    updateLevelBadge(data.level, data.status);
    updateReason(data.reason, data.incident_type);
    updateScoreBars(data.scores);
    updateExplainability(data);

    const audioLabel = document.getElementById("audio-label");
    if (audioLabel) audioLabel.textContent = data.audio_label || "—";

    const cloudStatus = document.getElementById("cloud-status-text");
    const cloudDot = document.getElementById("cloud-dot");
    if (cloudStatus && cloudDot) {
        if (data.cloud_online) {
            cloudStatus.textContent = "Online & Monitoring";
            cloudStatus.style.color = "#4ade80";
            cloudDot.style.background = "#22c55e";
        } else {
            cloudStatus.textContent = "Offline (Local AI Mode)";
            cloudStatus.style.color = "#8a9bb8";
            cloudDot.style.background = "#8a9bb8";
        }
    }

    if (data.level >= 4 && data.dispatch_package) {
        showDispatchPanel(data.dispatch_package);
    } else {
        hideDispatchPanel();
    }

    if (lastLevel !== null && lastLevel !== data.level) {
        addEventLog(
            `Level changed: ${LEVEL_CONFIG[lastLevel].label} → ${LEVEL_CONFIG[data.level].label}`,
            data.reason,
            data.level
        );
    }
}

function updateExplainability(data) {
    // Top factors
    const factorsEl = document.getElementById("top-factors-list");
    if (factorsEl && Array.isArray(data.top_factors) && data.top_factors.length > 0) {
        factorsEl.innerHTML = data.top_factors.map(f => {
            const pct = Math.round((f.contribution || 0) * 100);
            const barColor = pct > 30 ? "#ef4444" : pct > 15 ? "#facc15" : "#3b82f6";
            return `<div style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.2rem;">
                <span style="width:64px;color:#8a9bb8;text-transform:capitalize;">${f.name}</span>
                <div style="flex:1;height:5px;background:#1e2535;border-radius:3px;overflow:hidden;">
                  <div style="width:${pct}%;height:100%;background:${barColor};border-radius:3px;transition:width 0.5s;"></div>
                </div>
                <span style="color:#c5d0e8;width:30px;text-align:right;">${pct}%</span>
            </div>`;
        }).join("");
    }

    // Uncertainty / confidence badge
    const badge = document.getElementById("uncertainty-badge");
    if (badge && typeof data.uncertainty === "number") {
        const u = data.uncertainty;
        let label, color;
        if (u < 0.25)       { label = "HIGH";   color = "#22c55e"; }
        else if (u < 0.55)  { label = "MEDIUM"; color = "#facc15"; }
        else                { label = "LOW";    color = "#f97316"; }
        badge.textContent = label;
        badge.style.color = color;
    }

    // Confidence band
    const bandEl = document.getElementById("tci-band");
    if (bandEl && Array.isArray(data.confidence_band)) {
        const lo = (data.confidence_band[0] * 100).toFixed(1);
        const hi = (data.confidence_band[1] * 100).toFixed(1);
        bandEl.textContent = `${lo}% – ${hi}%`;
    }

    // Latency + queue depth
    const latAvg = document.getElementById("lat-avg");
    const latP95 = document.getElementById("lat-p95");
    const queueD = document.getElementById("queue-depth");
    if (latAvg && data.latency_avg !== undefined) latAvg.textContent = data.latency_avg;
    if (latP95 && data.latency_p95 !== undefined) latP95.textContent = data.latency_p95;
    if (queueD && data.queue_depth !== undefined) queueD.textContent = data.queue_depth;
}

function updateHealthStatus(data) {
    const dotCam = document.getElementById("dot-cam");
    const dotCloud = document.getElementById("dot-cloud");
    // Assume camera is online if we are receiving frames/scores (heuristics based on ws data)
    if (dotCam) dotCam.className = "status-dot online";
    if (dotCloud) {
        dotCloud.className = "status-dot " + (data.cloud_online ? "online" : "");
    }
}

function updateGauge(tci) {
    const arc = document.getElementById("gauge-arc");
    const val = document.getElementById("gauge-value");
    if (!arc || !val) return;
    
    const percent = Math.min(Math.max(tci, 0), 1) * 100;
    // Length of the arc path is roughly 283
    const offset = 283 - (283 * percent / 100);
    arc.style.strokeDashoffset = offset;
    val.textContent = percent.toFixed(1) + "%";
}

function updateLevelBadge(level, status) {
    const badge = document.getElementById("level-badge");
    const incBadge = document.getElementById("incident-badge");
    if (!badge) return;
    
    const config = LEVEL_CONFIG[level] || LEVEL_CONFIG[1];
    badge.textContent = config.label;
    badge.style.color = config.color;
    
    if (incBadge) {
        incBadge.textContent = status;
        incBadge.style.borderColor = config.color;
        incBadge.style.color = config.color;
    }
    
    // Update theme accent
    document.body.style.setProperty("--threat-accent", config.color);
    const arc = document.getElementById("gauge-arc");
    if (arc) arc.style.stroke = config.color;
}

function updateReason(reason, type) {
    const el = document.getElementById("reason-text");
    if (el) el.textContent = `[${type.toUpperCase()}] ${reason}`;
}

function updateScoreBars(scores) {
    if (!scores) return;
    SCORE_KEYS.forEach(key => {
        const val = scores[key] || 0;
        const bar = document.getElementById("score-" + key);
        if (bar) {
            bar.style.width = (val * 100) + "%";
            // change color based on value
            if (val > 0.6) bar.style.background = "#ef4444";
            else if (val > 0.3) bar.style.background = "#facc15";
            else bar.style.background = "#3b82f6";
        }
    });
}

function showDispatchPanel(pkg) {
    const panel = document.getElementById("dispatch-panel");
    if (!panel) return;
    if (panel.dataset.dismissed === pkg.id) return; // user dismissed this specific package
    
    panel.style.display = "block";
    document.getElementById("dp-incident").textContent = pkg.incident_type.toUpperCase();
    document.getElementById("dp-address").textContent = pkg.user_address;
    document.getElementById("dp-tci").textContent = pkg.tci.toFixed(2);
    document.getElementById("dp-authority").textContent = pkg.recommended_authority;
    document.getElementById("dp-id").value = pkg.id;
}

function hideDispatchPanel() {
    const panel = document.getElementById("dispatch-panel");
    if (panel) panel.style.display = "none";
}

function dismissDispatch() {
    const panel = document.getElementById("dispatch-panel");
    const id = document.getElementById("dp-id").value;
    if (panel) {
        panel.style.display = "none";
        panel.dataset.dismissed = id;
    }
}

function onDispatchClick(authority) {
    const pkg_id = document.getElementById("dp-id").value;
    if (!pkg_id) return;
    
    fetch(`/api/dispatch/${pkg_id}/send/${authority}`, { method: "POST" })
        .then(r => r.json())
        .then(d => {
            if (d.status === "sent") {
                alert("Dispatched successfully to " + authority);
                dismissDispatch();
            } else {
                alert("Dispatch failed");
            }
        })
        .catch(e => alert("Error: " + e));
}

function addEventLog(title, detail, level) {
    const container = document.getElementById("log-container");
    if (!container) return;
    
    const entry = document.createElement("div");
    entry.className = "log-entry";
    
    let color = "#3b82f6";
    if (level) {
        color = (LEVEL_CONFIG[level] || {}).color || color;
        entry.style.borderLeftColor = color;
    }
    
    entry.innerHTML = `<strong style="color:${color}">${title}</strong><span>${detail}</span>`;
    container.prepend(entry);
    
    if (container.children.length > 50) {
        container.removeChild(container.lastChild);
    }
}