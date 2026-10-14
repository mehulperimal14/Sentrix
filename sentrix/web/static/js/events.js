// static/js/events.js
// Operator verdicts on the Event Log. Every labelled incident is training
// data (CLAUDE.md M8). "False alarm" goes through the SOS service: with an
// open assistance request it needs the SOS PIN, asked for here when the
// console answers 403 pin_invalid.

"use strict";

(() => {
    const S = window.Sentrix;

    function describeVerdict(verdict, by) {
        const who = by === "lc:device_admin" ? "device admin"
            : (by || "").startsWith("portal:") ? "you (Portal)" : (by || "");
        return [verdict === "TRUE" ? "Real" : "False alarm", who, new Date().toLocaleString()]
            .filter(Boolean).join(" · ");
    }

    async function sendVerdict(row, verdict, pin) {
        const body = new FormData();
        body.append("verdict", verdict);
        const note = row.querySelector("[data-verdict-note]");
        if (note && note.value.trim()) body.append("note", note.value.trim());
        if (pin) body.append("pin", pin);
        const result = await S.api(`/api/incidents/${encodeURIComponent(row.dataset.incident)}/verdict`,
                                   { method: "POST", form: body });
        const meta = row.querySelector("[data-verdict-meta]");
        const pinRow = row.querySelector("[data-verdict-pin-row]");
        if (result.ok) {
            row.querySelectorAll(".verdict-btn").forEach(button => {
                button.classList.toggle("active", button.dataset.verdict === verdict);
            });
            if (meta) meta.textContent = describeVerdict(verdict, result.data.by);
            if (pinRow) {
                pinRow.classList.add("hidden");
                const input = pinRow.querySelector("[data-verdict-pin]");
                if (input) input.value = "";
            }
            return;
        }
        if (result.code === "pin_invalid" && pinRow) {
            pinRow.classList.remove("hidden");
            const input = pinRow.querySelector("[data-verdict-pin]");
            if (input) input.focus();
            if (meta) meta.textContent = pin ? "Wrong PIN." : "This incident has an open assistance request: enter the SOS PIN.";
            return;
        }
        if (meta) {
            meta.textContent = result.code === "pin_locked"
                ? "Too many wrong PINs; try again in 10 minutes."
                : "Could not save the verdict: " + (result.detail || "error");
        }
    }

    document.addEventListener("DOMContentLoaded", () => {
        document.querySelectorAll(".verdict-btn").forEach(button => {
            button.addEventListener("click", () => {
                sendVerdict(button.closest("tr"), button.dataset.verdict, null);
            });
        });
        document.querySelectorAll("[data-verdict-pin-confirm]").forEach(button => {
            button.addEventListener("click", () => {
                const row = button.closest("tr");
                const input = row.querySelector("[data-verdict-pin]");
                sendVerdict(row, "FALSE", input ? input.value.trim() : "");
            });
        });
    });
})();
