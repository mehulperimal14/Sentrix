// static/js/recording.js
// The dashboard's "Record video" card (D20): the device admin on the
// LAN records the camera's raw frames, with the microphone's sound while one
// is connected, then downloads or deletes the finished files. The owner
// through the Portal starts, stops and lists recordings (start and stop need
// a fresh step-up); the tunnel refuses downloads and deletes
// (sentrix/link/policy.py), so there the list has no Download or Delete
// buttons (download_here and can_delete false) and the note names the
// console's address on the home Wi-Fi (lan_url).
//   GET    /api/recordings          every 1 s (Portal: 2 s) while recording or saving, else 10 s
//   POST   /api/recordings/start    {label}
//   POST   /api/recordings/stop
//   DELETE /api/recordings/<name>   after a confirm click
//   downloads: plain links to /media/recording/<file> on this device, except
//   in a browser running on a Raspberry Pi itself (download_here false): the
//   video would land on its own SD card, so the card names the address to
//   open on a laptop instead.
// Device strings reach the page through textContent only (Sentrix.h).

"use strict";

(() => {
    const S = window.Sentrix;
    // Through the Portal every poll is a tunnel round trip and a row in the
    // device's remote audit: every 2 s while recording there, 1 s on the LAN.
    const TUNNELLED = document.body && document.body.dataset.tunnelled === "1";
    const FAST_MS = TUNNELLED ? 2000 : 1000;
    const SLOW_MS = 10000;
    const CONFIRM_MS = 5000;
    // Why a recording stopped by itself (the recorder's stop reasons).
    const STOP_REASONS = {
        max_length: "it reached its maximum length",
        disk_full: "the device's disk is nearly full",
        camera_lost: "the camera stopped sending frames",
        max_size: "the video reached its size limit (1.9 GB)",
        shutdown: "SENTRIX was shutting down",
        error: "of an error",
        interrupted: "SENTRIX stopped unexpectedly",
    };

    let card = null;
    let timer = null;
    let generation = 0;
    let busy = false;
    let last = null;              // the last GET /api/recordings body
    let listKey = "";             // what the list shows, to redraw only on change
    let pendingDelete = "";       // a recording waiting for its confirm click
    let confirmTimer = null;

    function part(name) {
        return card.querySelector(`[data-record-${name}]`);
    }

    function size(bytes) {
        const value = Number(bytes || 0);
        const gb = 1024 * 1024 * 1024;
        return value >= gb ? `${(value / gb).toFixed(2)} GB` : `${(value / (1024 * 1024)).toFixed(1)} MB`;
    }

    function micLine(mic) {
        return mic && mic.available && mic.device ? `with sound: ${mic.device}` : "no microphone - video only";
    }

    function failureText(result) {
        if (result.status === 404 && !result.code) return "Recording is not installed on this build.";
        if (result.code === "rate_limited") return "Too many starts: wait a minute.";
        if (result.code === "step_up_required") return "Re-enter your password on the SENTRIX Portal first.";
        if (result.code === "forbidden_role" || result.code === "lc_only") {
            return "Not allowed from here: through the Portal only the owner starts and stops "
                + "recordings, and downloads and deletes happen on the home Wi-Fi.";
        }
        return result.detail || "The request failed.";
    }

    function statusText(data) {
        const status = data.status || {};
        if (!status.enabled) return "Recording is turned off on this device (SENTRIX_RECORDING_ENABLED=0).";
        if (status.recording) {
            return `● REC ${S.formatRemaining(status.seconds)} / ${S.formatRemaining(status.max_seconds)}`
                + ` · ${size(status.bytes)} · ${status.frames || 0} frames · ${micLine(data.microphone)}`;
        }
        if (status.finalizing) return "Finishing the files…";
        const parts = ["Ready", micLine(data.microphone)];
        if (data.free_bytes !== null && data.free_bytes !== undefined) parts.push(`${size(data.free_bytes)} free`);
        let text = parts.join(" · ");
        if (data.microphone && data.microphone.available && !data.ffmpeg) {
            text += ". ffmpeg is not installed: the sound is saved as a separate .wav";
        }
        if (status.last_error) {
            text += `. Last recording: ${status.last_error}`;
        } else if (status.name && STOP_REASONS[status.stop_reason]) {
            text += `. The last recording (${status.name}) stopped because ${STOP_REASONS[status.stop_reason]}.`;
        }
        return text;
    }

    function laptopAddress(data) {
        // The server's URL first: through the Portal this page's own port is the Portal's.
        if (data.lan_url) return data.lan_url;
        const port = window.location.port || "8000";
        return data.device_address ? `http://${data.device_address}:${port}` : "this device's address";
    }

    function noteText(data) {
        const days = Number(data.keep_days || 0);
        const kept = days > 0 ? `Recordings are deleted after ${days} days.`
                              : "Recordings are kept until you delete them.";
        if (data.via === "portal") {
            return "Start and stop recordings here. Download and delete them on the home Wi-Fi: open "
                + laptopAddress(data) + " on a computer there and sign in with the device password. "
                + kept;
        }
        if (data.download_here === false) {
            return "Downloads are off in this browser: it runs on the device itself, and the videos "
                + "would fill its SD card. To download, open " + laptopAddress(data)
                + " on your laptop (same Wi-Fi) and sign in. " + kept;
        }
        return "Download from a computer on the same network as the device; not through the Portal. " + kept;
    }

    function download(file, text) {
        return S.h("a", { href: S.url("/media/recording/" + encodeURIComponent(file)), download: file,
                          class: "btn btn-sm btn-outline", text });
    }

    function renderList(items, downloadHere, canDelete) {
        const key = JSON.stringify([items, pendingDelete, downloadHere, canDelete]);
        if (key === listKey) return;
        listKey = key;
        const list = part("list");
        if (!items.length) {
            list.replaceChildren(S.h("li", { class: "muted", text: "No recordings yet." }));
            return;
        }
        list.replaceChildren(...items.map(item => {
            const files = item.files || {};
            const facts = [S.localTime(item.created_at), S.formatRemaining(item.seconds), size(item.bytes),
                           item.has_audio ? "sound: yes" : "sound: no"];
            if (item.interrupted) facts.push("recovered after an interruption");
            const confirming = pendingDelete === item.name;
            return S.h("li", { class: "record-item" },
                S.h("div", { class: "record-name", text: item.name }),
                S.h("div", { class: "record-meta", text: facts.join(" · ") }),
                S.h("div", { class: "record-actions" },
                    files.video && downloadHere ? download(files.video, "Download video") : null,
                    files.audio && downloadHere ? download(files.audio, "Download sound") : null,
                    canDelete ? S.h("button", { type: "button", dataset: { recordDelete: item.name },
                                                class: "btn btn-sm btn-danger-outline"
                                                    + (confirming ? " armed" : ""),
                                                text: confirming ? "Confirm delete" : "Delete" })
                              : null));
        }));
    }

    function render(data) {
        if (!data) return;
        const status = data.status || {};
        const button = part("toggle");
        const label = part("label");
        button.textContent = status.recording ? "Stop recording" : "Start recording";
        button.classList.toggle("recording", !!status.recording);
        button.disabled = busy || !!status.finalizing || (!status.recording && !status.enabled);
        label.disabled = !!status.recording || !!status.finalizing;
        part("status").textContent = statusText(data);
        part("status").classList.toggle("recording", !!status.recording);
        part("note").textContent = noteText(data);
        renderList(Array.isArray(data.items) ? data.items : [], data.download_here !== false,
                   data.can_delete !== false);
    }

    async function refresh() {
        const mine = ++generation;
        window.clearTimeout(timer);
        const result = await S.api("/api/recordings");
        if (mine !== generation) return;          // a newer refresh is on its way
        if (result.ok) {
            last = result.data;
            render(last);
        } else {
            part("status").textContent = failureText(result);
            if (result.status === 401) return;    // signed out: the banner says so
        }
        const status = (last && last.status) || {};
        timer = window.setTimeout(refresh, status.recording || status.finalizing ? FAST_MS : SLOW_MS);
    }

    async function toggle() {
        if (busy) return;
        const recording = !!(last && last.status && last.status.recording);
        const message = part("message");
        const label = part("label").value.trim();
        busy = true;
        part("toggle").disabled = true;
        const result = recording
            ? await S.api("/api/recordings/stop", { method: "POST" })
            : await S.api("/api/recordings/start", { method: "POST", json: { label } });
        busy = false;
        if (result.ok) {
            S.message(message, true, recording ? "Stopped: the files are being saved." : "Recording.");
            last = Object.assign({}, last || {}, { status: result.data });
            render(last);
        } else {
            S.message(message, false, failureText(result));
        }
        refresh();
    }

    async function remove(name, button) {
        window.clearTimeout(confirmTimer);
        if (pendingDelete !== name) {
            pendingDelete = name;
            confirmTimer = window.setTimeout(() => { pendingDelete = ""; render(last); }, CONFIRM_MS);
            render(last);
            return;
        }
        pendingDelete = "";
        button.disabled = true;
        const result = await S.api("/api/recordings/" + encodeURIComponent(name), { method: "DELETE" });
        S.message(part("message"), result.ok, result.ok ? `Deleted ${name}.` : failureText(result));
        refresh();
    }

    document.addEventListener("DOMContentLoaded", () => {
        card = document.querySelector("[data-record-root]");
        if (!card) return;
        part("toggle").addEventListener("click", toggle);
        part("label").addEventListener("keydown", event => {
            if (event.key === "Enter") toggle();
        });
        part("list").addEventListener("click", event => {
            const button = event.target.closest("[data-record-delete]");
            if (button) remove(button.dataset.recordDelete, button);
        });
        refresh();
    });
})();
