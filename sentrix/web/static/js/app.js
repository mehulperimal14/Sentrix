// static/js/app.js
// Shared by every console page (docs/7 §9):
//   * URLs: the same pages are served on the LAN and by the SENTRIX Portal
//     under /d/<device id>, so every fetch, link, image and the websocket is
//     built with Sentrix.url() from <body data-base>.
//   * Every state-changing fetch carries the X-CSRF-Token header from
//     <meta name="csrf-token"> (Sentrix.api does it).
//   * One reconnecting websocket (/ws/threat) feeds every page through
//     Sentrix.onState(); close code 1008 means "sign in again".
//   * Nav dots (AI, camera, mic, Portal link, modem), "remote view: N", the
//     "no outbound channel" banner and the SOS prompt / banner / countdown
//     chip, all driven by the live state.
// Server and device strings reach the page through textContent only (no
// innerHTML), and handlers are attached here, never inline (strict CSP).

"use strict";

const LEVEL_CONFIG = {
    1: { label: "Level 1 – Normal",     color: "#22c55e", bg: "rgba(34, 197, 94, 0.1)" },
    2: { label: "Level 2 – Suspicious", color: "#facc15", bg: "rgba(250, 204, 21, 0.1)" },
    3: { label: "Level 3 – Elevated",   color: "#f97316", bg: "rgba(249, 115, 22, 0.1)" },
    4: { label: "Level 4 – High",       color: "#ef4444", bg: "rgba(239, 68, 68, 0.1)" },
    5: { label: "Level 5 – Critical",   color: "#dc2626", bg: "rgba(220, 38, 38, 0.1)" },
};

const Sentrix = (() => {
    const body = document.body;
    const base = body.dataset.base || "";
    const meta = document.querySelector('meta[name="csrf-token"]');
    const csrf = meta ? (meta.getAttribute("content") || "") : "";
    const caps = new Set((body.dataset.caps || "").split(/\s+/).filter(Boolean));
    const stateListeners = [];
    const sosListeners = [];
    let lastState = null;
    let expired = false;

    // ── URLs, requests, DOM ─────────────────────────────────────────────────
    function url(path) {
        const text = String(path || "");
        return base + (text.startsWith("/") ? text : "/" + text);
    }

    // State blocks carry base-relative paths ("media/snapshot/x.jpg").
    function rel(path) {
        return url(String(path || "").replace(/^\/+/, ""));
    }

    function can(capability) {
        return caps.has(capability);
    }

    function errorText(res, data) {
        if (data && typeof data.detail === "string") return data.detail;
        if (data && Array.isArray(data.detail)) {
            return data.detail.map(item => item.msg || "").filter(Boolean).join("; ");
        }
        if (data && typeof data.message === "string" && data.message) return data.message;
        return res.status ? `${res.status} ${res.statusText || ""}`.trim() : "network error";
    }

    // fetch() under the base, with the CSRF header on every state change.
    // Resolves to {ok, status, data, code, detail}; never throws.
    async function api(path, options = {}) {
        const method = (options.method || "GET").toUpperCase();
        const headers = Object.assign({ "Accept": "application/json" }, options.headers || {});
        const init = { method, headers, credentials: "same-origin" };
        if (method !== "GET" && method !== "HEAD") {
            headers["X-CSRF-Token"] = csrf;
            headers["X-Requested-With"] = "fetch";
        }
        if (options.json !== undefined) {
            headers["Content-Type"] = "application/json";
            init.body = JSON.stringify(options.json);
        } else if (options.form) {
            init.body = options.form;
        }
        let res;
        try {
            res = await fetch(url(path), init);
        } catch (err) {
            return { ok: false, status: 0, data: {}, code: "network",
                     detail: "The console cannot be reached right now." };
        }
        let data = {};
        try {
            data = await res.json();
        } catch (err) {
            data = {};
        }
        if (res.status === 401) sessionExpired();
        return { ok: res.ok, status: res.status, data: data || {},
                 code: (data && data.code) || "", detail: res.ok ? "" : errorText(res, data) };
    }

    // Element builder: text goes in as text nodes, never as markup.
    function h(tag, props, ...children) {
        const node = document.createElement(tag);
        for (const [key, value] of Object.entries(props || {})) {
            if (value === undefined || value === null || value === false) continue;
            if (key === "class") node.className = value;
            else if (key === "text") node.textContent = String(value);
            else if (key === "dataset") Object.assign(node.dataset, value);
            else node.setAttribute(key, value === true ? "" : String(value));
        }
        for (const child of children.flat()) {
            if (child === undefined || child === null || child === false) continue;
            node.append(child instanceof Node ? child : document.createTextNode(String(child)));
        }
        return node;
    }

    function setText(id, value) {
        const node = document.getElementById(id);
        if (node) node.textContent = value === undefined || value === null ? "" : String(value);
        return node;
    }

    function show(node, visible) {
        if (node) node.classList.toggle("hidden", !visible);
    }

    function localTime(iso) {
        if (!iso) return "";
        const when = new Date(iso);
        return isNaN(when.getTime()) ? String(iso) : when.toLocaleString();
    }

    function applyLocalTimes(root) {
        (root || document).querySelectorAll(".local-time[data-iso]").forEach(node => {
            if (node.dataset.iso) node.textContent = localTime(node.dataset.iso);
        });
    }

    function formatRemaining(seconds) {
        if (seconds === null || seconds === undefined || !isFinite(seconds)) return "--:--";
        const total = Math.max(0, Math.floor(seconds));
        const hours = Math.floor(total / 3600);
        const minutes = Math.floor((total % 3600) / 60);
        const pad = n => String(n).padStart(2, "0");
        return hours ? `${hours}:${pad(minutes)}:${pad(total % 60)}` : `${pad(minutes)}:${pad(total % 60)}`;
    }

    function message(node, ok, text) {
        if (!node) return;
        node.textContent = text || "";
        node.classList.toggle("ok", !!ok && !!text);
        node.classList.toggle("bad", !ok && !!text);
    }

    // ── live state ──────────────────────────────────────────────────────────
    function sessionExpired() {
        if (expired) return;
        expired = true;
        show(document.getElementById("session-banner"), true);
    }

    let socket = null;
    let retryDelay = 1000;

    function connect() {
        if (expired) return;
        if (socket && (socket.readyState === WebSocket.OPEN ||
                       socket.readyState === WebSocket.CONNECTING)) return;
        const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
        socket = new WebSocket(`${scheme}//${window.location.host}${url("/ws/threat")}`);
        socket.addEventListener("open", () => { retryDelay = 1000; });
        socket.addEventListener("message", event => {
            let data;
            try {
                data = JSON.parse(event.data);
            } catch (err) {
                return;
            }
            lastState = data;
            for (const listener of stateListeners) {
                try { listener(data); } catch (err) { console.error(err); }
            }
        });
        socket.addEventListener("close", event => {
            socket = null;
            if (event.code === 1008) {          // signed out or session expired
                sessionExpired();
                return;
            }
            setTimeout(connect, retryDelay);   // 1 s, 2 s, 4 s ... 30 s
            retryDelay = Math.min(retryDelay * 2, 30000);
        });
        socket.addEventListener("error", () => { if (socket) socket.close(); });
    }

    function onState(listener) {
        stateListeners.push(listener);
        if (lastState) listener(lastState);
    }

    // ── nav dots and banners ────────────────────────────────────────────────
    function setDot(id, mode, title) {
        const dot = document.getElementById(id);
        if (!dot) return;
        dot.className = "status-dot" + (mode ? " " + mode : "");
        if (title) dot.title = title;
    }

    function linkDot(link) {
        if (!link || !link.enabled) return ["idle", "Portal link: disabled"];
        if (link.connected) {
            return ["online", "Portal link: connected" + (link.claimed === false ? " (not claimed yet)" : "")];
        }
        return ["", "Portal link: " + (link.state || "offline")];
    }

    function modemDot(channels) {
        const modem = channels && channels.modem;
        if (!modem) return ["idle", "4G modem: not configured"];
        if (!modem.present) return ["", "4G modem: not detected"];
        if (!modem.registered) return ["warn", "4G modem: not registered on the network"];
        const signal = modem.signal_dbm !== null && modem.signal_dbm !== undefined
            ? `, ${modem.signal_dbm} dBm` : "";
        return ["online", "4G modem: registered" + signal];
    }

    function updateHealthStatus(data) {
        if ("camera_online" in data) {
            setDot("dot-cam", data.camera_online ? "online" : "",
                   data.camera_online ? "Camera: online" : "Camera: no signal");
        }
        if ("mic_available" in data) {
            setDot("dot-mic", data.mic_available ? "online" : "",
                   data.mic_available ? "Microphone: listening" : "Microphone: off");
        }
        const running = (data.ai_hz || 0) > 0;
        setDot("dot-ai", running ? "online" : "", running ? "AI engine: running" : "AI engine: not running");
        if ("link" in data) setDot("dot-link", ...linkDot(data.link));
        if ("channels" in data) setDot("dot-modem", ...modemDot(data.channels));
    }

    function updateBanners(data) {
        const viewers = Number(data.remote_viewers || 0);
        show(document.getElementById("remote-view"), viewers > 0);
        setText("remote-view-count", viewers);
        if (data.channels) {
            show(document.getElementById("channels-banner"), !!data.channels.warn);
        }
    }

    // ── SOS prompt, banner and countdown chip (state.sos) ───────────────────
    const MINIMISED_KEY = "sentrix.sos.minimised";
    const PROMPTING = new Set(["DRAFTED", "PENDING"]);
    // forced: opened from the banner/chip, kept open until minimised.
    const sos = { block: null, deadline: null, remainingSeen: null, forced: false };

    function storedMinimised() {
        try {
            return window.sessionStorage.getItem(MINIMISED_KEY) || "";
        } catch (err) {
            return "";
        }
    }

    function storeMinimised(value) {
        try {
            window.sessionStorage.setItem(MINIMISED_KEY, value);
        } catch (err) {
            // Storage blocked (private window): the prompt simply reopens.
        }
    }

    // A level rise (urgency HIGH, L4/L5) reopens a minimised prompt.
    function promptKey(block) {
        return block ? `${block.request_id}|${block.level}|${block.urgency}` : "";
    }

    function actionsOf(block) {
        return new Set((block && Array.isArray(block.actions)) ? block.actions : []);
    }

    function isPrompting(block) {
        return !!block && (PROMPTING.has(block.state) || actionsOf(block).has("approve"));
    }

    function evidenceWording(block) {
        if (!block || !block.evidence_state || block.incident_type === "intrusion") return "";
        return block.evidence_state === "corroborated"
            ? "confirmed by two different models" : "possible (one camera check)";
    }

    function remainingSeconds() {
        return sos.deadline === null ? null : (sos.deadline - Date.now()) / 1000;
    }

    function countdownText() {
        const block = sos.block;
        if (!block) return "";
        const remaining = remainingSeconds();
        return remaining === null ? (block.label || block.state || "") : formatRemaining(remaining);
    }

    function applyActions(block) {
        const actions = actionsOf(block);
        const callFirst = !!block && ["weapon", "fire"].includes(block.incident_type);
        document.querySelectorAll("[data-sos-actions]").forEach(box => {
            const root = box.closest("[data-sos-root]");
            if (root && root.dataset.requestId) return;          // a row of the SOS page
            box.classList.toggle("call-first", callFirst);
            box.querySelectorAll("[data-sos-show]").forEach(node => {
                show(node, actions.has(node.dataset.sosShow));
            });
            if (block && box.dataset.requestId !== block.request_id) {
                box.dataset.requestId = block.request_id;   // a new request: reset the form
                const preferred = block.authority || block.recommended_authority;
                box.querySelectorAll("[data-sos-authority]").forEach(input => {
                    input.checked = input.value === preferred;
                });
                const pinRow = box.querySelector("[data-sos-pin-row]");
                if (pinRow) pinRow.classList.add("hidden");
                message(box.querySelector("[data-sos-message]"), true, "");
            }
        });
    }

    function renderPrompt(force) {
        const prompt = document.getElementById("sos-prompt");
        if (!prompt) return;
        const block = sos.block;
        if (force) sos.forced = true;
        const visible = !!block && can("sos") &&
            (sos.forced || (isPrompting(block) && storedMinimised() !== promptKey(block)));
        if (!block) {
            show(prompt, false);
            return;
        }
        if (visible) {
            setText("sos-prompt-title", isPrompting(block) ? "Do you need assistance?"
                                                           : "Assistance request");
            const parts = [block.short_id, (block.incident_type || "").toUpperCase(),
                           block.level ? `Level ${block.level}` : "", evidenceWording(block)];
            setText("sos-prompt-sub", parts.filter(Boolean).join(" · "));
            setText("sos-prompt-label", block.label || block.state || "");
            const snapshot = document.getElementById("sos-prompt-snapshot");
            if (snapshot) {
                if (block.snapshot) {
                    const src = rel(block.snapshot);
                    if (snapshot.getAttribute("src") !== src) snapshot.setAttribute("src", src);
                }
                show(snapshot, !!block.snapshot && can("media"));
            }
            const text = setText("sos-prompt-text", block.sos_text || "");
            show(text, !!block.sos_text);
        }
        show(prompt, visible);
    }

    function tick() {
        const text = countdownText();
        const chip = document.getElementById("sos-chip");
        if (chip && sos.block) chip.textContent = `SOS ${text}`;
        setText("sos-banner-countdown", text);
        setText("sos-prompt-countdown", text);
        document.querySelectorAll("[data-sos-countdown]").forEach(node => { node.textContent = text; });
    }

    // The deadline comes from expires_at (absolute, so a block that is not
    // republished every second still counts down); remaining_s is used only
    // without it, and only when its value changes.
    function updateDeadline(block) {
        if (!block) {
            sos.deadline = null;
            sos.remainingSeen = null;
            return;
        }
        const at = block.expires_at ? Date.parse(block.expires_at) : NaN;
        if (!isNaN(at)) {
            sos.deadline = at;
        } else if (typeof block.remaining_s === "number") {
            if (block.remaining_s !== sos.remainingSeen) {
                sos.remainingSeen = block.remaining_s;
                sos.deadline = Date.now() + block.remaining_s * 1000;
            }
        } else {
            sos.deadline = null;                     // help path: approved requests never lapse
        }
    }

    function updateSos(block) {
        const active = !!(block && block.active && block.request_id);
        if (!active || (sos.block && sos.block.request_id !== block.request_id)) sos.forced = false;
        sos.block = active ? block : null;
        updateDeadline(sos.block);
        show(document.getElementById("sos-chip"), active);
        show(document.getElementById("sos-banner"), active);
        if (active) {
            setText("sos-banner-id", block.short_id || "");
            setText("sos-banner-label", block.label || block.state || "");
        }
        applyActions(sos.block);
        renderPrompt(false);
        tick();
        for (const listener of sosListeners) {
            try { listener(sos.block); } catch (err) { console.error(err); }
        }
    }

    function onSos(listener) {
        sosListeners.push(listener);
        listener(sos.block);
    }

    function respond(requestId, action, payload) {
        const path = action === "help_now" ? "help-now" : action;
        return api(`/api/sos/requests/${encodeURIComponent(requestId)}/${path}`,
                   { method: "POST", json: payload || {} });
    }

    const DONE = {
        approve: "Your contacts and the SENTRIX desk are being alerted.",
        checking: "Reminders paused. The request still lapses on time.",
        decline: "Marked as false alarm.",
        safe: "The desk has been told you are safe.",
        help_now: "A new assistance request was opened.",
    };

    function failureText(result) {
        switch (result.code) {
        case "pin_invalid": return "Wrong PIN.";
        case "pin_locked": return "Too many wrong PINs; try again in 10 minutes. " +
                                  "“Alert my contacts and the SENTRIX desk” still works.";
        case "unsupported": return "The SOS service is off on this unit.";
        case "conflict": return "This request has already moved on: " + (result.detail || "reload the page.");
        default:
            if (result.status === 404) return "The SOS service did not answer (not installed on this build).";
            return result.detail || "That did not work.";
        }
    }

    function chosenAuthority(box) {
        const checked = box ? box.querySelector("[data-sos-authority]:checked") : null;
        if (checked) return checked.value;
        const block = sos.block;
        return (block && (block.authority || block.recommended_authority)) || "POLICE";
    }

    async function runAction(button) {
        const box = button.closest("[data-sos-actions]");
        const root = button.closest("[data-sos-root]");
        const action = button.dataset.sosAction;
        const requestId = (root && root.dataset.requestId) || (sos.block && sos.block.request_id);
        const status = box ? box.querySelector("[data-sos-message]") : null;
        if (!requestId) {
            message(status, false, "There is no open request.");
            return;
        }
        const payload = {};
        if (action === "approve") payload.authority = chosenAuthority(box);
        if (action === "decline" || action === "safe") {
            const input = box ? box.querySelector("[data-sos-pin]") : null;
            payload.pin = input ? input.value.trim() : "";
            if (!payload.pin) {
                message(status, false, "Enter the SOS PIN.");
                return;
            }
        }
        button.disabled = true;
        const result = await respond(requestId, action, payload);
        button.disabled = false;
        if (!result.ok) {
            message(status, false, failureText(result));
            return;
        }
        message(status, true, (result.data && result.data.message) || DONE[action] || "Done.");
        if (action === "decline" || action === "safe") {
            const row = box.querySelector("[data-sos-pin-row]");
            const input = box.querySelector("[data-sos-pin]");
            if (input) input.value = "";
            if (row) row.classList.add("hidden");
        }
        document.dispatchEvent(new CustomEvent("sentrix:sos-answered", { detail: { requestId, action } }));
    }

    function openPin(button) {
        const box = button.closest("[data-sos-actions]");
        const row = box ? box.querySelector("[data-sos-pin-row]") : null;
        if (!row) return;
        const confirm = row.querySelector("[data-sos-pin-confirm]");
        if (confirm) {
            confirm.dataset.sosAction = button.dataset.sosPinFor;
            confirm.textContent = button.dataset.sosPinFor === "safe" ? "Confirm I'm safe"
                                                                       : "Confirm false alarm";
        }
        row.classList.remove("hidden");
        const input = row.querySelector("[data-sos-pin]");
        if (input) input.focus();
    }

    document.addEventListener("click", event => {
        const target = event.target instanceof Element ? event.target : null;
        if (!target) return;
        const pinFor = target.closest("[data-sos-pin-for]");
        if (pinFor) {
            openPin(pinFor);
            return;
        }
        const cancel = target.closest("[data-sos-pin-cancel]");
        if (cancel) {
            const row = cancel.closest("[data-sos-pin-row]");
            if (row) row.classList.add("hidden");
            return;
        }
        const call = target.closest("[data-sos-call]");
        if (call && call.getAttribute("aria-disabled") === "true") {
            event.preventDefault();
            return;
        }
        const action = target.closest("[data-sos-action]");
        if (action) {
            event.preventDefault();
            runAction(action);
        }
    });

    function minimisePrompt() {
        sos.forced = false;
        if (sos.block) storeMinimised(promptKey(sos.block));
        show(document.getElementById("sos-prompt"), false);
    }

    function openPrompt() {
        storeMinimised("");
        renderPrompt(true);
    }

    // ── page start ──────────────────────────────────────────────────────────
    function logout(event) {
        if (event) event.preventDefault();
        api("/logout", { method: "POST" }).then(() => {
            window.location.assign(url("/login"));
        });
    }

    document.addEventListener("DOMContentLoaded", () => {
        applyLocalTimes(document);
        const minimise = document.getElementById("sos-prompt-minimise");
        if (minimise) minimise.addEventListener("click", minimisePrompt);
        const bannerOpen = document.getElementById("sos-banner-open");
        if (bannerOpen) bannerOpen.addEventListener("click", openPrompt);
        const chip = document.getElementById("sos-chip");
        if (chip) chip.addEventListener("click", openPrompt);
        document.addEventListener("keydown", event => {
            const prompt = document.getElementById("sos-prompt");
            if (event.key === "Escape" && prompt && !prompt.classList.contains("hidden")) minimisePrompt();
        });
        const logoutLink = document.getElementById("logout-link");
        if (logoutLink) logoutLink.addEventListener("click", logout);
        const logoutConfirm = document.getElementById("logout-confirm");
        if (logoutConfirm) logoutConfirm.addEventListener("click", logout);

        onState(data => {
            updateHealthStatus(data);
            updateBanners(data);
            updateSos(data.sos || null);
        });
        connect();
        window.setInterval(tick, 1000);

        // Set the dots before the first websocket message arrives.
        api("/api/health").then(result => {
            if (!result.ok) return;
            const health = result.data;
            updateHealthStatus({ camera_online: health.camera, mic_available: health.audio,
                                 ai_hz: health.ai_hz });
        });
    });

    return {
        url, rel, api, can, h, setText, show, message, localTime, applyLocalTimes,
        formatRemaining, onState, onSos, levels: LEVEL_CONFIG,
        sos: {
            respond,
            failureText,
            block: () => sos.block,
            remaining: remainingSeconds,
            countdownText,
            evidenceWording,
            openPrompt,
        },
    };
})();

window.Sentrix = Sentrix;
