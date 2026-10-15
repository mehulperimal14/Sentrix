// static/js/speaker.js
// "Speak to the room" (decision D16): the owner (through the Portal) or the
// device admin (on the LAN) sends a preset or a short message that the
// device speaks with offline text-to-speech. POST /api/speaker/say through
// Sentrix.api (base-aware URL, CSRF header); everything is written with
// textContent / DOM nodes. Who may speak is enforced by the device (the
// tunnel path table: owner + step-up), not by this script.

"use strict";

(() => {
    const S = window.Sentrix;

    function statusText(status) {
        if (!status || !status.enabled) return "Speaker is off on this device (SENTRIX_SPEAKER_ENABLED=0).";
        if (!status.real) return `Speaker: ${status.backend} — no text-to-speech installed, messages are only logged.`;
        return `Speaker: ${status.backend}${status.speaking ? " · speaking" : " · ready"}` +
               ` · at most ${status.per_minute} messages a minute`;
    }

    function failure(result) {
        if (result.code === "step_up_required") return "Re-enter your password on the Portal first, then try again.";
        if (result.code === "forbidden_role") return "Only the owner can speak through the device.";
        if (result.code === "rate_limited") return "Too many messages: wait a moment and try again.";
        return result.detail || "Not spoken.";
    }

    async function say(body, buttons) {
        const message = document.getElementById("speaker-message");
        buttons.forEach(b => { b.disabled = true; });
        const result = await S.api("/api/speaker/say", { method: "POST", json: body });
        buttons.forEach(b => { b.disabled = false; });
        if (result.ok) {
            S.message(message, true, `Spoken: “${result.data.text}”` +
                      (result.data.message ? ` (${result.data.message})` : ""));
            const input = document.getElementById("speaker-text");
            if (input && body.text) input.value = "";
        } else {
            S.message(message, false, failure(result));
        }
        refresh();
    }

    function allButtons() {
        return Array.from(document.querySelectorAll("#speaker-card button"));
    }

    function renderPresets(presets) {
        const box = document.getElementById("speaker-presets");
        if (!box || box.dataset.ready) return;
        box.dataset.ready = "1";
        box.replaceChildren(...(presets || []).map(preset => {
            const button = S.h("button", { type: "button", class: "btn btn-sm btn-cancel",
                                           text: preset.text, dataset: { preset: preset.id } });
            button.addEventListener("click", () => say({ preset_id: preset.id }, allButtons()));
            return button;
        }));
    }

    async function refresh() {
        const result = await S.api("/api/speaker");
        const line = document.getElementById("speaker-status");
        if (!result.ok) {
            if (line) line.textContent = result.status === 404
                ? "The speaker is not installed on this build." : "Speaker status unavailable.";
            return;
        }
        if (line) line.textContent = statusText(result.data);
        renderPresets(result.data.presets);
    }

    document.addEventListener("DOMContentLoaded", () => {
        if (!document.getElementById("speaker-card")) return;
        const send = document.getElementById("speaker-send");
        const input = document.getElementById("speaker-text");
        if (send && input) {
            send.addEventListener("click", () => {
                const text = input.value.trim();
                if (text) say({ text }, allButtons());
            });
            input.addEventListener("keydown", event => {
                if (event.key === "Enter") {
                    event.preventDefault();
                    send.click();
                }
            });
        }
        refresh();
    });
})();
