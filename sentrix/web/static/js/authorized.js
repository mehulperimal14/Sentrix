// static/js/authorized.js
// Access Control: enrol from the monitoring camera (its own frame, polled
// as a preview), upload photos, delete a face. Results are written with
// textContent: file names and the server's reasons are data, not markup.

"use strict";

(() => {
    const S = window.Sentrix;
    let previewTimer = null;

    function showStatus(lines) {
        const box = document.getElementById("enrol-status");
        if (!box) return;
        box.replaceChildren(...lines.map(line =>
            S.h("div", { class: "status-line " + (line.ok ? "ok" : "bad"), text: line.text })));
        S.show(box, lines.length > 0);
    }

    function refreshPreview() {
        const preview = document.getElementById("capture-preview");
        if (preview) preview.setAttribute("src", S.url("/api/authorized/preview.jpg") + "?t=" + Date.now());
    }

    function toggleCapture(open) {
        const section = document.getElementById("capture-section");
        if (!section) return;
        const visible = open === undefined ? section.classList.contains("hidden") : open;
        S.show(section, visible);
        if (visible) {
            refreshPreview();
            if (!previewTimer) previewTimer = window.setInterval(refreshPreview, 500);
        } else if (previewTimer) {
            window.clearInterval(previewTimer);
            previewTimer = null;
        }
    }

    async function captureFace() {
        const nameInput = document.getElementById("capture-name");
        const person = nameInput ? nameInput.value.trim() : "";
        if (!person) {
            showStatus([{ ok: false, text: "Enter a name first." }]);
            return;
        }
        const button = document.getElementById("capture-btn");
        button.disabled = true;
        const body = new FormData();
        body.append("person", person);
        const result = await S.api("/api/authorized/capture", { method: "POST", form: body });
        button.disabled = false;
        if (result.ok && result.data.status === "ok") {
            showStatus([{ ok: true, text: `Enrolled ${result.data.person} (${result.data.filename}). ` +
                                          "Capture another angle; the gallery refreshes in a moment." }]);
            window.setTimeout(() => window.location.reload(), 2500);
        } else {
            showStatus([{ ok: false, text: `Not enrolled: ${result.data.detail || result.detail}` }]);
        }
    }

    async function uploadFaces(input) {
        if (!input.files || input.files.length === 0) return;
        const nameInput = document.getElementById("upload-name");
        const person = nameInput ? nameInput.value.trim() : "";
        const lines = [];
        for (const file of input.files) {
            const body = new FormData();
            body.append("file", file);
            if (person) body.append("person", person);   // else the file name is the name
            const result = await S.api("/authorized/upload", { method: "POST", form: body });
            if (result.ok && result.data.status === "ok") {
                lines.push({ ok: true, text: `${file.name}: enrolled as ${result.data.person}` });
            } else {
                lines.push({ ok: false, text: `${file.name}: ${result.data.detail || result.detail}` });
            }
            showStatus(lines);
        }
        input.value = "";
        if (lines.some(line => line.ok)) window.setTimeout(() => window.location.reload(), 1500);
    }

    async function deleteFace(button) {
        const filename = button.dataset.deleteFace;
        if (!window.confirm(`Remove ${filename}?`)) return;
        const result = await S.api(`/api/authorized/${encodeURIComponent(filename)}`, { method: "DELETE" });
        if (result.ok) {
            window.location.reload();
        } else {
            showStatus([{ ok: false, text: `Delete failed: ${result.detail}` }]);
        }
    }

    document.addEventListener("DOMContentLoaded", () => {
        const toggle = document.getElementById("capture-toggle");
        if (toggle) toggle.addEventListener("click", () => toggleCapture());
        const close = document.getElementById("capture-close");
        if (close) close.addEventListener("click", () => toggleCapture(false));
        const capture = document.getElementById("capture-btn");
        if (capture) capture.addEventListener("click", captureFace);
        const fileInput = document.getElementById("file-input");
        const upload = document.getElementById("upload-btn");
        if (upload && fileInput) {
            upload.addEventListener("click", () => fileInput.click());
            fileInput.addEventListener("change", () => uploadFaces(fileInput));
        }
        document.querySelectorAll("[data-delete-face]").forEach(button => {
            button.addEventListener("click", () => deleteFace(button));
        });
    });
})();
