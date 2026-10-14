// static/js/dispatch.js
// "Send now" on a prepared package (the console answers 409 for SENT,
// CANCELLED and EXPIRED packages) and "Request help now" on a request that
// lapsed unanswered or was declined: it opens a new, linked request.

"use strict";

(() => {
    const S = window.Sentrix;

    async function sendNow(button) {
        const row = button.closest("tr");
        const select = row ? row.querySelector("[data-send-authority]") : null;
        const authority = select ? select.value : "POLICE";
        const status = document.getElementById("dispatch-message");
        if (!window.confirm(`Send this package with ${authority} as the context?`)) return;
        button.disabled = true;
        const result = await S.api(
            `/api/dispatch/${encodeURIComponent(button.dataset.sendPackage)}/send/${encodeURIComponent(authority)}`,
            { method: "POST" });
        button.disabled = false;
        if (result.ok && result.data.status === "sent") {
            window.location.reload();
            return;
        }
        const why = result.ok ? (result.data.detail || result.data.message) : result.detail;
        S.message(status, false, "Not sent: " + (why || "unknown error"));
    }

    async function helpNow(button) {
        const status = document.getElementById("dispatch-message");
        if (!window.confirm("Open a new assistance request from this one?")) return;
        button.disabled = true;
        const result = await S.sos.respond(button.dataset.helpNow, "help_now", {});
        button.disabled = false;
        if (result.ok) {
            S.message(status, true, "A new assistance request was opened.");
            window.setTimeout(() => window.location.reload(), 1200);
        } else {
            S.message(status, false, S.sos.failureText(result));
        }
    }

    document.addEventListener("DOMContentLoaded", () => {
        document.querySelectorAll("[data-send-package]").forEach(button => {
            button.addEventListener("click", () => sendNow(button));
        });
        document.querySelectorAll("[data-help-now]").forEach(button => {
            button.addEventListener("click", () => helpNow(button));
        });
    });
})();
