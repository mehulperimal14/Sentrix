// static/js/dashboard.js
// The Threat Command Console: gauge, engine scores, scene classifier,
// pipeline, threat analysis, the SOS panel (the former Emergency Dispatch
// panel) and the Event Log. Live data comes from Sentrix.onState (one shared
// websocket); everything is written with textContent / DOM nodes.

"use strict";

(() => {
    const S = window.Sentrix;
    const SCORE_KEYS = ["vision", "audio", "motion", "behaviour", "identity", "weapon",
                        "fire", "theft", "harmful", "is_night"];
    const NO_MODEL = new Set(["theft", "harmful"]);   // no engine produces these yet
    const SENDABLE = new Set(["PENDING", "FAILED"]);

    let latest = null;
    let lastAlarmLevel = null;
    let historyLoaded = false;

    // ── gauge, badges, headline ─────────────────────────────────────────────
    function updateGauge(tci) {
        const arc = document.getElementById("gauge-arc");
        const value = document.getElementById("gauge-value");
        if (!arc || !value) return;
        const percent = Math.min(Math.max(Number(tci) || 0, 0), 1) * 100;
        arc.style.strokeDashoffset = 283 - (283 * percent / 100);   // arc length ~283
        value.textContent = percent.toFixed(1) + "%";
    }

    function updateLevelBadge(level, status) {
        const config = S.levels[level] || S.levels[1];
        const badge = document.getElementById("level-badge");
        if (badge) {
            badge.textContent = config.label;
            badge.style.color = config.color;
        }
        const incident = document.getElementById("incident-badge");
        if (incident) {
            incident.textContent = status || "";
            incident.style.borderColor = config.color;
            incident.style.color = config.color;
        }
        document.body.style.setProperty("--threat-accent", config.color);
        const arc = document.getElementById("gauge-arc");
        if (arc) arc.style.stroke = config.color;
    }

    function updateReason(reason, type) {
        S.setText("reason-text", `[${String(type || "normal").toUpperCase()}] ${reason || ""}`);
    }

    // ── engine scores ───────────────────────────────────────────────────────
    function updateScoreBars(scores) {
        if (!scores) return;
        SCORE_KEYS.forEach(key => {
            const bar = document.getElementById("score-" + key);
            const label = document.getElementById("score-" + key + "-value");
            if (NO_MODEL.has(key)) return;
            const value = Math.min(Math.max(Number(scores[key]) || 0, 0), 1);
            if (bar) {
                bar.style.width = (value * 100) + "%";
                bar.style.background = value > 0.6 ? "#ef4444" : value > 0.3 ? "#facc15" : "#3b82f6";
            }
            if (label) {
                label.textContent = key === "is_night" ? (value >= 0.5 ? "night" : "day")
                                                       : Math.round(value * 100) + "%";
            }
        });
    }

    // Scene classifier verdict, class probabilities (window-averaged), the
    // per-class thresholds and whether this tick ran the model.
    function updateScene(scene) {
        const label = document.getElementById("scene-label");
        const list = document.getElementById("scene-probs");
        const meta = document.getElementById("scene-meta");
        if (!label || !list) return;
        if (!scene) {
            label.textContent = "not installed";
            label.style.color = "";
            list.replaceChildren();
            if (meta) meta.textContent = "";
            return;
        }
        label.textContent = scene.label;
        label.style.color = scene.label === "normal" ? "" : "#ef4444";
        const rows = Object.entries(scene.probs || {}).map(([name, value]) => {
            const bar = S.h("div", { class: "score-bar" });
            bar.style.width = (value * 100) + "%";
            bar.style.background = name === "normal" ? "#3b82f6"
                : (value > 0.5 ? "#ef4444" : (value > 0.25 ? "#facc15" : "#3b82f6"));
            return S.h("div", { class: "score-row" },
                       S.h("span", { class: "score-label", text: name }),
                       S.h("div", { class: "score-track" }, bar),
                       S.h("span", { class: "score-value", text: Math.round(value * 100) + "%" }));
        });
        list.replaceChildren(...rows);
        if (meta) {
            const thresholds = Object.entries(scene.thresholds || {})
                .map(([name, value]) => `${name} ${Number(value).toFixed(2)}`).join(" · ");
            const fresh = scene.fresh === false ? "held from an earlier tick" : "this tick";
            meta.textContent = (thresholds ? `thresholds: ${thresholds} · ` : "") + fresh;
        }
    }

    // ── pipeline card ───────────────────────────────────────────────────────
    function updatePipeline(data) {
        S.setText("rate-video", (data.video_fps || 0).toFixed(0));
        S.setText("rate-ai", (data.ai_hz || 0).toFixed(1));
        S.setText("frame-age", data.frame_age_ms != null ? Math.round(data.frame_age_ms) : "—");
        const text = document.getElementById("ai-status-text");
        const dot = document.getElementById("ai-dot");
        if (text && dot) {
            let words = "AI not running", color = "#8a9bb8";
            if (!data.camera_online) {
                words = "No camera signal";
                color = "#f97316";
            } else if ((data.ai_hz || 0) > 0) {
                words = "Analysing live video";
                color = "#22c55e";
            }
            text.textContent = words;
            text.style.color = color === "#22c55e" ? "#4ade80" : color;
            dot.style.background = color;
        }
        const link = data.link || {};
        let state = "Portal link: disabled";
        if (link.enabled) {
            state = link.connected ? "Portal link: connected" : `Portal link: ${link.state || "offline"}`;
        }
        S.setText("link-state", state);
        const remote = document.getElementById("remote-video-label");
        if (remote) {
            const rv = data.remote_video;
            S.show(remote, !!rv);
            if (rv) remote.textContent = `remote ${Math.round(rv.fps || 0)} fps · relay delay ${Math.round(rv.delay_ms || 0)} ms`;
        }
    }

    // ── threat analysis ─────────────────────────────────────────────────────
    function updateExplainability(data) {
        const factors = document.getElementById("top-factors-list");
        if (factors && Array.isArray(data.top_factors) && data.top_factors.length > 0) {
            factors.replaceChildren(...data.top_factors.map(factor => {
                const pct = Math.round((factor.contribution || 0) * 100);
                const bar = S.h("div", { class: "factor-bar" });
                bar.style.width = pct + "%";
                bar.style.background = pct > 30 ? "#ef4444" : pct > 15 ? "#facc15" : "#3b82f6";
                return S.h("div", { class: "factor-row" },
                           S.h("span", { class: "factor-name", text: factor.name }),
                           S.h("div", { class: "factor-track" }, bar),
                           S.h("span", { class: "factor-pct", text: pct + "%" }));
            }));
        }
        // Evidence state of the typed class: the TCI is a transparent rule, so
        // what matters is how many independent sources back it (CLAUDE.md M1).
        const badge = document.getElementById("uncertainty-badge");
        if (badge) {
            const colors = { corroborated: "#ef4444", confirmed: "#f97316",
                             unconfirmed: "#8a9bb8", none: "#22c55e" };
            const state = data.evidence_state || "none";
            badge.textContent = state.toUpperCase();
            badge.style.color = colors[state] || "#8a9bb8";
        }
        if (Array.isArray(data.confidence_band)) {
            const lo = (data.confidence_band[0] * 100).toFixed(1);
            const hi = (data.confidence_band[1] * 100).toFixed(1);
            S.setText("tci-band", `${lo}% – ${hi}%`);
        }
        if (data.latency_avg !== undefined) S.setText("lat-avg", data.latency_avg);
        if (data.latency_p95 !== undefined) S.setText("lat-p95", data.latency_p95);
        if (data.queue_depth !== undefined) S.setText("queue-depth", data.queue_depth);
    }

    // ── SOS panel ───────────────────────────────────────────────────────────
    // An open assistance request (state.sos) shows here with its countdown
    // and answers. With SOS off, the current incident's pending dispatch
    // package (Level 4+) shows instead, as before. "Dismiss" hides it for
    // that request/package; the nav chip and the banner stay.
    const panel = { mode: null, id: "", dismissed: "", pkg: null, fetchedAt: 0, fetchedFor: "" };

    function refreshPackage(force) {
        if (!S.can("dispatch")) return;
        const now = Date.now();
        if (!force && now - panel.fetchedAt < 5000) return;
        panel.fetchedAt = now;
        // In SOS mode: the request's own package in any status, so the
        // address stays for the life of the request (not only while PENDING).
        const block = S.sos.block();
        const query = block && block.request_id
            ? "?request_id=" + encodeURIComponent(block.request_id) : "";
        S.api("/api/dispatch/latest" + query).then(result => {
            panel.pkg = result.ok && result.data && result.data.id ? result.data : null;
            renderPanel();
        });
    }

    function renderPanel() {
        const node = document.getElementById("dispatch-panel");
        if (!node || !latest) return;
        const block = S.sos.block();
        const alarm = latest.alarm || {};
        const level = alarm.level || latest.level || 1;
        const pkg = panel.pkg;
        let mode = null;
        let id = "";
        if (block) {
            mode = "sos";
            id = block.request_id;
            if (panel.fetchedFor !== id) {          // the request's package: address
                panel.fetchedFor = id;
                refreshPackage(true);
            }
        } else if (pkg && level >= 4 && (!alarm.incident_id || pkg.incident_id === alarm.incident_id)) {
            mode = "package";
            id = pkg.id;
        }
        if (!block && level >= 4) refreshPackage(false);
        panel.mode = mode;
        panel.id = id;
        if (!mode || panel.dismissed === id) {
            S.show(node, false);
            return;
        }
        S.show(node, true);
        const ownPkg = pkg && (mode === "package" || pkg.request_id === id) ? pkg : null;
        const type = (mode === "sos" ? block.incident_type : pkg.incident_type) || "";
        const shownLevel = mode === "sos" ? block.level : pkg.level;
        S.setText("dp-title", mode === "sos" ? `ASSISTANCE REQUEST ${block.short_id || ""}`.trim()
                                             : "EMERGENCY DISPATCH READY");
        S.setText("dp-state", mode === "sos"
            ? [block.label || block.state, S.sos.evidenceWording(block)].filter(Boolean).join(" · ")
            : `Package ${pkg.status || ""}`);
        S.setText("dp-incident", type.toUpperCase() + (shownLevel ? ` · L${shownLevel}` : ""));
        const tci = mode === "sos" ? (alarm.peak_tci || latest.tci) : pkg.tci;
        S.setText("dp-tci", tci !== undefined && tci !== null ? Number(tci).toFixed(2) : "—");
        S.setText("dp-address", (ownPkg && ownPkg.user_address) || "—");
        S.setText("dp-authority", (mode === "sos" ? (block.authority || block.recommended_authority)
                                                  : pkg.recommended_authority) || "—");
        const hidden = document.getElementById("dp-id");
        if (hidden) hidden.value = id;
        const snapshot = document.getElementById("dp-snapshot");
        if (snapshot) {
            const path = mode === "sos" ? block.snapshot : null;
            if (path && snapshot.getAttribute("src") !== S.rel(path)) snapshot.setAttribute("src", S.rel(path));
            S.show(snapshot, !!path && S.can("media"));
        }
        const actions = new Set((block && block.actions) || []);
        const canSend = mode === "sos" ? actions.has("approve") : SENDABLE.has(pkg.status);
        node.querySelectorAll("[data-dispatch-authority]").forEach(button => S.show(button, canSend));
        S.show(document.getElementById("dp-sos-actions"), mode === "sos");
        S.setText("dp-note", mode === "sos"
            ? "Dispatch sends the prepared SOS to your confirmed contacts and the SENTRIX desk with that context. SENTRIX does not contact 112 itself."
            : "Dispatch sends this package as a text message to the configured alert number (guarded: a dry run unless live alerts are switched on).");
    }

    async function onDispatch(authority) {
        const status = document.getElementById("dp-message");
        const block = S.sos.block();
        let result;
        if (panel.mode === "sos" && block) {
            result = await S.sos.respond(block.request_id, "approve", { authority });
            if (!result.ok) {
                S.message(status, false, S.sos.failureText(result));
                return;
            }
            S.message(status, true, `Your contacts and the SENTRIX desk are being alerted (${authority}).`);
            return;
        }
        if (panel.mode !== "package" || !panel.pkg) return;
        result = await S.api(`/api/dispatch/${encodeURIComponent(panel.pkg.id)}/send/${authority}`,
                             { method: "POST" });
        if (result.ok && result.data.status === "sent") {
            S.message(status, true, `Sent with ${authority} as the context.`);
        } else {
            const why = result.ok ? (result.data.detail || result.data.message) : result.detail;
            S.message(status, false, "Not sent: " + (why || "unknown error"));
        }
        refreshPackage(true);
    }

    // ── event log ───────────────────────────────────────────────────────────
    function levelLabel(level) {
        return (S.levels[level] || S.levels[1]).label;
    }

    function addEventLog(title, detail, level) {
        const container = document.getElementById("log-container");
        if (!container) return;
        const color = (S.levels[level] || {}).color || "#3b82f6";
        const heading = S.h("strong", { text: title });
        heading.style.color = color;
        const entry = S.h("div", { class: "log-entry" }, heading, S.h("span", { text: detail }));
        entry.style.borderLeftColor = color;
        container.prepend(entry);
        while (container.children.length > 50) container.removeChild(container.lastChild);
    }

    function describe(reason, type, at) {
        return [reason, type && type !== "normal" ? type : "", S.localTime(at)].filter(Boolean).join(" · ");
    }

    function loadHistory() {
        if (!S.can("pages")) {                     // the Desk view has no history API
            historyLoaded = true;
            return;
        }
        S.api("/api/level-history").then(result => {
            const items = result.ok && Array.isArray(result.data) ? result.data : [];
            if (items.length) {
                const init = document.querySelector("#log-container .log-init");
                if (init) init.remove();
            }
            items.forEach(item => addEventLog(
                `Level changed: ${levelLabel(item.previous)} → ${levelLabel(item.level)}`,
                describe(item.reason, item.incident_type, item.at), item.level));
            if (items.length && lastAlarmLevel === null) lastAlarmLevel = items[items.length - 1].level;
            historyLoaded = true;
        });
    }

    // ── live updates ────────────────────────────────────────────────────────
    function updateDashboard(data) {
        latest = data;
        updateGauge(data.tci);
        // The badge and headline describe the committed alarm (its incident's
        // peak), not the latest tick; the gauge still shows the live TCI.
        const alarm = data.alarm || { level: data.level, status: data.status,
                                      incident_type: data.incident_type, headline: data.reason };
        updateLevelBadge(alarm.level, alarm.status);
        if (alarm.level > 1) {
            let text = alarm.headline || data.reason;
            if (alarm.deescalate_in !== null && alarm.deescalate_in !== undefined) {
                text += ` — clear for ${Math.ceil(alarm.deescalate_in)} s more to step down`;
            }
            updateReason(text, alarm.incident_type);
        } else {
            updateReason(data.reason, data.incident_type);
        }
        updateScoreBars(data.scores);
        updateExplainability(data);
        S.setText("audio-label", data.audio_label || "—");
        updateScene(data.scene);
        updatePipeline(data);
        renderPanel();

        if (historyLoaded && lastAlarmLevel !== null && lastAlarmLevel !== alarm.level) {
            addEventLog(`Level changed: ${levelLabel(lastAlarmLevel)} → ${levelLabel(alarm.level)}`,
                        describe(alarm.headline || data.reason, alarm.incident_type, new Date().toISOString()),
                        alarm.level);
        }
        if (historyLoaded || lastAlarmLevel === null) lastAlarmLevel = alarm.level;
    }

    document.addEventListener("DOMContentLoaded", () => {
        document.querySelectorAll("[data-dispatch-authority]").forEach(button => {
            button.addEventListener("click", () => onDispatch(button.dataset.dispatchAuthority));
        });
        const dismiss = document.getElementById("dp-dismiss");
        if (dismiss) {
            dismiss.addEventListener("click", () => {
                panel.dismissed = panel.id;
                S.show(document.getElementById("dispatch-panel"), false);
            });
        }
        document.addEventListener("sentrix:sos-answered", () => refreshPackage(true));
        loadHistory();
        S.onState(updateDashboard);
        S.onSos(() => renderPanel());
    });
})();
