// static/js/node.js
// The Node page: device telemetry and I/O (GET /api/node, track T-HW), the
// Portal link and the channels/modem blocks (live state; /api/link and
// /api/channels when installed), system health (GET /api/health), latency,
// HOME/ARMED (POST /api/mode) and the confirmed 1-second siren test
// (POST /api/node/siren-test). Read-only configuration from /api/config.

"use strict";

(() => {
    const S = window.Sentrix;
    let nodeApi = true;          // false once /api/node answered 404
    let lastRender = 0;

    function fmt(value) {
        if (value === null || value === undefined || value === "") return "—";
        if (typeof value === "boolean") return value ? "yes" : "no";
        if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(2);
        if (typeof value === "object") return JSON.stringify(value);
        return String(value);
    }

    // The siren block is a status dict: shown as one short line.
    function sirenText(siren) {
        if (!siren || typeof siren !== "object") return siren;
        const parts = [String(siren.backend || "siren") + (siren.real === false ? " (mock)" : "")];
        if (siren.armed !== undefined && siren.armed !== null) parts.push(siren.armed ? "armed" : "disarmed");
        if (siren.on !== undefined) parts.push(siren.on ? "ON" : "off");
        return parts.join(" · ");
    }

    function fillTable(id, rows) {
        const table = document.getElementById(id);
        if (!table) return;
        const shown = rows.filter(row => row !== null);
        table.replaceChildren(S.h("tbody", {}, shown.length
            ? shown.map(([key, value]) => S.h("tr", {}, S.h("th", { text: key }), S.h("td", { text: fmt(value) })))
            : [S.h("tr", {}, S.h("td", { class: "muted", text: "Nothing reported." }))]));
    }

    // The talk-down speaker (D16): backend, whether it makes real sound.
    function speakerText(speaker) {
        if (!speaker || typeof speaker !== "object") return null;
        if (!speaker.enabled) return "off (SENTRIX_SPEAKER_ENABLED=0)";
        const parts = [String(speaker.backend || "none") + (speaker.real ? "" : " (no sound)")];
        if (speaker.speaking) parts.push("speaking");
        return parts.join(", ");
    }

    function renderNode(node) {
        node = node || {};
        fillTable("node-telemetry", [
            ["Profile", node.profile],
            ["CPU temperature (°C)", node.cpu_temp_c],
            ["Throttled", node.throttled],
            ["Under-voltage", node.under_voltage],
            ["5 V rail (V)", node.ext5v_v],
            ["Mains power", node.mains],
            ["Uptime (s)", node.uptime_s],
            ["Updated", S.localTime(node.updated_at)],
        ]);
        const gpio = node.gpio || {};
        const leds = gpio.leds || {};
        fillTable("node-gpio", [
            ["Siren", gpio.siren !== undefined ? sirenText(gpio.siren) : node.siren_armed],
            ["LED green / amber / red", gpio.leds ? `${fmt(leds.green)} / ${fmt(leds.amber)} / ${fmt(leds.red)}` : null],
            ["Buzzer", gpio.buzzer],
            ["Motion (PIR)", gpio.pir !== undefined ? gpio.pir : node.pir],
            ["Door", gpio.door !== undefined ? gpio.door : node.door],
            ["Mains sense", gpio.mains !== undefined ? gpio.mains : node.mains],
            ["Speaker (talk-down)", speakerText(node.speaker)],
        ]);
        const note = document.getElementById("node-telemetry-note");
        if (note) {
            note.textContent = !nodeApi ? "The node telemetry service is not installed on this build."
                : !node.updated_at ? "No telemetry yet (the node service is off: SENTRIX_NODE_ENABLED=0)." : "";
        }
    }

    function renderLink(link, viewers) {
        link = link || {};
        fillTable("node-link", [
            ["Enabled", link.enabled],
            ["State", link.state],
            ["Connected", link.connected],
            ["Claimed", link.claimed],
            ["Device id", link.device_id],
            ["Portal", link.portal],
            ["Since", S.localTime(link.since)],
            ["Last message", S.localTime(link.last_message_at)],
            ["Outbox depth", link.outbox_depth],
            ["Profile version", link.profile_version],
            ["Remote viewers", viewers],
        ]);
    }

    function renderChannels(channels) {
        channels = channels || {};
        const modem = channels.modem;
        const rows = [
            ["Router running", channels.enabled],
            ["Something can leave", channels.outbound_ready],
            ["Live sending", channels.live],
            ["Legacy Twilio notifier", channels.legacy_twilio],
            ["Outbox depth", channels.outbox_depth],
            ["Last error", channels.last_error],
        ];
        for (const [name, value] of Object.entries(channels.channels || {})) {
            const ready = value && typeof value === "object" ? value.ready : value;
            rows.push([`Channel ${name}`, ready]);
        }
        rows.push(["Modem", modem ? (modem.present ? "present" : "not detected") : "not configured"]);
        if (modem) {
            rows.push(["Modem registered", modem.registered], ["Signal (dBm)", modem.signal_dbm],
                      ["SMS today", modem.sms_today]);
        }
        fillTable("node-channels", rows);
    }

    function renderHealth(health) {
        const guard = health.send_guard || {};
        const rows = [
            ["Camera", health.camera], ["Camera source", health.camera_source],
            ["Video fps", health.video_fps], ["AI Hz", health.ai_hz],
            ["Frame age (ms)", health.frame_age_ms], ["Detector", health.detector],
            ["Scene model", health.scene_model], ["Scene device", health.scene_device],
            ["Face recognition", health.identity_backend || "unavailable"],
            ["Identity failing closed", health.identity_failing_closed],
            ["Enrolled faces", health.enrolled_faces], ["Microphone", health.audio],
            ["Fusion", health.fusion_source], ["Siren", health.siren_backend],
            ["Siren is real hardware", health.siren_is_real],
            ["Legacy notifier online", health.notifier_online],
            ["Evidence key persistent", health.evidence_key_persistent],
            ["Armed", health.armed], ["Test mode", health.test_mode],
            ["Live alerts switch", guard.live_alerts], ["Session", health.session],
        ];
        for (const [name, service] of Object.entries(health.services || {})) {
            rows.push([`Service ${name}`, service && typeof service === "object"
                ? (service.status || (service.running ? "running" : "stopped")) : service]);
        }
        fillTable("node-health", rows);
        fillTable("node-latency", Object.entries(health.latency || {}).map(([name, stat]) =>
            [name, `${fmt(stat.mean_ms)} mean · ${fmt(stat.p95_ms)} p95 · n=${fmt(stat.n)}`]));
    }

    async function refreshNode() {
        if (!nodeApi) return;
        const result = await S.api("/api/node");
        if (result.status === 404) {
            nodeApi = false;
            return;
        }
        if (result.ok) renderNode(result.data);
    }

    async function refreshHealth() {
        const result = await S.api("/api/health");
        if (result.ok) renderHealth(result.data);
    }

    async function loadConfig() {
        const box = document.getElementById("node-config");
        const result = await S.api("/api/config");
        if (box) box.textContent = result.ok ? JSON.stringify(result.data, null, 2)
                                             : `Unavailable (${result.detail}).`;
    }

    function renderMode(mode) {
        if (!mode) return;
        const word = mode.armed ? "ARMED" : "HOME";
        S.setText("mode-value", word);
        S.setText("node-mode", word);
        S.setText("mode-meta", mode.changed_by
            ? `· set by ${mode.changed_by}${mode.changed_at ? " at " + S.localTime(mode.changed_at) : ""}` : "");
    }

    async function setMode(armed) {
        const status = document.getElementById("mode-message");
        const pinInput = document.getElementById("mode-pin");
        const payload = { armed };
        if (!armed) payload.pin = pinInput ? pinInput.value.trim() : "";
        const result = await S.api("/api/mode", { method: "POST", json: payload });
        if (pinInput) pinInput.value = "";
        if (result.ok) {
            renderMode(result.data.mode);
            S.message(status, true, armed ? "Armed." : "Disarmed.");
        } else {
            S.message(status, false, result.code === "pin_invalid" ? "Wrong PIN."
                : result.code === "pin_locked" ? "Too many wrong PINs; try again in 10 minutes."
                : result.detail);
        }
    }

    async function sirenTest(button) {
        const status = document.getElementById("siren-test-message");
        const confirm = document.getElementById("siren-test-confirm");
        button.disabled = true;
        const result = await S.api("/api/node/siren-test", { method: "POST" });
        if (confirm) confirm.checked = false;
        if (result.ok) {
            S.message(status, true, (result.data && result.data.message) || "The siren sounded for 1 second.");
            return;
        }
        S.message(status, false,
            result.status === 404 ? "The siren test is not installed on this build."
            : result.code === "rate_limited" ? "One test a minute: wait and try again."
            : result.code === "step_up_required" ? "Re-enter your password on the SENTRIX Portal first."
            : result.detail);
    }

    async function buzzerTest(button) {
        const status = document.getElementById("buzzer-test-message");
        button.disabled = true;
        const result = await S.api("/api/node/buzzer-test", { method: "POST" });
        setTimeout(() => { button.disabled = false; }, 3000);
        if (result.ok) {
            S.message(status, true, (result.data && result.data.message) || "Two chirps.");
            return;
        }
        S.message(status, false,
            result.status === 404 ? "The buzzer test is not installed on this build."
            : result.code === "rate_limited" ? "One test every few seconds."
            : result.detail);
    }

    function renderClip(clip) {
        // state.clip (track T-CLIP): the 4-second clip engine, or the tick loop.
        clip = clip || {};
        if (clip.mode !== "clip") {
            fillTable("node-clip", [["Mode", "tick loop (SENTRIX_PIPELINE_MODE=tick)"]]);
            return;
        }
        const compute = clip.compute_ms || {};
        const lane = clip.fast_lane || null;
        fillTable("node-clip", [
            ["Mode", `clip (${fmt(clip.window_s)} s window, ${fmt(clip.hop_s)} s hop)`],
            ["Runtime / threads", `${fmt(clip.runtime)} / ${fmt(clip.threads)}`],
            ["Clip compute p50 / p95 (ms)", `${fmt(compute.p50)} / ${fmt(compute.p95)}`],
            ["Budget (ms)", clip.budget_ms],
            ["Clips / dropped", `${fmt(clip.clips)} / ${fmt(clip.dropped)}`],
            ["Fast lane", lane ? "on" : "off"],
            ["Fast-lane decisions", clip.fast_lane_decisions],
        ]);
    }

    function onState(data) {
        const now = Date.now();
        if (now - lastRender < 1000) return;
        lastRender = now;
        if (!nodeApi) renderNode(data.node);
        renderLink(data.link, data.remote_viewers || 0);
        renderChannels(data.channels);
        renderMode(data.mode);
        renderClip(data.clip);
    }

    document.addEventListener("DOMContentLoaded", () => {
        const confirm = document.getElementById("siren-test-confirm");
        const siren = document.getElementById("siren-test-btn");
        if (confirm && siren) {
            confirm.addEventListener("change", () => { siren.disabled = !confirm.checked; });
            siren.addEventListener("click", () => sirenTest(siren));
        }
        const buzzer = document.getElementById("buzzer-test-btn");
        if (buzzer) buzzer.addEventListener("click", () => buzzerTest(buzzer));
        const arm = document.getElementById("mode-arm");
        if (arm) arm.addEventListener("click", () => setMode(true));
        const disarm = document.getElementById("mode-disarm");
        if (disarm) disarm.addEventListener("click", () => setMode(false));

        renderNode(null);
        S.onState(onState);
        refreshNode().then(() => { if (!nodeApi) renderNode(null); });
        refreshHealth();
        loadConfig();
        window.setInterval(refreshNode, 5000);
        window.setInterval(refreshHealth, 10000);
    });
})();
