// static/js/sos.js
// The SOS page: the open request (live, from state.sos) with its countdown
// and answers, the request history (GET /api/sos/requests), one request's
// audit trail with its hash-chain status (GET /api/sos/requests/{id}) and
// the SOS preview (GET /api/sos/preview). The JSON API is the SOS service's
// (docs/7 §9.2); a build without it says so instead of failing silently.

"use strict";

(() => {
    const S = window.Sentrix;
    const PAGE_SIZE = 20;
    const REOPENABLE = new Set(["EXPIRED", "DECLINED", "CANCELLED"]);
    let offset = 0;
    let selected = "";

    // ── the open request ────────────────────────────────────────────────────
    function renderCurrent(block) {
        const card = document.getElementById("sos-current");
        S.show(document.getElementById("sos-current-empty"), !block);
        S.show(document.getElementById("sos-current-body"), !!block);
        if (card) card.classList.toggle("active", !!block);
        S.setText("sos-state-badge", block ? (block.label || block.state) : "no open request");
        if (!block) return;
        S.setText("sos-current-title", `${block.short_id || ""} · ${(block.incident_type || "").toUpperCase()}`);
        S.setText("sos-current-label", [block.label || block.state, S.sos.evidenceWording(block),
                                        block.repeat ? "repeat after a false alarm" : ""]
                                       .filter(Boolean).join(" · "));
        S.setText("sos-f-incident", block.incident_type || "—");
        S.setText("sos-f-level", block.level ? `L${block.level}` : "—");
        S.setText("sos-f-evidence", block.evidence_state || "—");
        S.setText("sos-f-urgency", block.urgency || "—");
        S.setText("sos-f-created", S.localTime(block.created_at) || "—");
        S.setText("sos-f-authority", block.authority || block.recommended_authority || "—");
        const snapshot = document.getElementById("sos-current-snapshot");
        if (snapshot) {
            if (block.snapshot && snapshot.getAttribute("src") !== S.rel(block.snapshot)) {
                snapshot.setAttribute("src", S.rel(block.snapshot));
            }
            S.show(snapshot, !!block.snapshot && S.can("media"));
        }
        const text = S.setText("sos-current-text", block.sos_text || "");
        S.show(text, !!block.sos_text);
    }

    // ── history ─────────────────────────────────────────────────────────────
    function requestsOf(data) {
        if (Array.isArray(data)) return data;
        if (data && Array.isArray(data.requests)) return data.requests;
        if (data && Array.isArray(data.items)) return data.items;
        return [];
    }

    function idOf(request) {
        return request.request_id || request.id || "";
    }

    function historyRow(request) {
        const id = idOf(request);
        const audit = S.h("button", { type: "button", class: "link-btn", text: "Audit trail",
                                      dataset: { auditRequest: id } });
        const cells = [
            S.h("td", { class: "mono", text: S.localTime(request.created_at) }),
            S.h("td", { class: "mono", text: request.short_id || id.slice(0, 8) }),
            S.h("td", { text: request.incident_type || "" }),
            S.h("td", { text: request.peak_level || request.level || request.trigger_level || "" }),
            S.h("td", { text: request.label || request.state || "" }),
            S.h("td", { class: "small", text: [S.localTime(request.responded_at),
                                               request.response_channel].filter(Boolean).join(" · ") || "—" }),
            S.h("td", { text: request.authority || "—" }),
        ];
        const actions = S.h("td", {}, audit);
        if (S.can("sos") && REOPENABLE.has(request.state)) {
            actions.append(" ", S.h("button", { type: "button", class: "btn btn-sm btn-approve",
                                                text: "Request help now", dataset: { helpNow: id } }));
        }
        cells.push(actions);
        const row = S.h("tr", { dataset: { request: id } }, cells);
        if (id === selected) row.classList.add("selected-row");
        return row;
    }

    async function loadHistory() {
        const body = document.getElementById("sos-history-body");
        const status = document.getElementById("sos-history-message");
        const result = await S.api(`/api/sos/requests?limit=${PAGE_SIZE + 1}&offset=${offset}`);
        if (!result.ok) {
            body.replaceChildren(S.h("tr", {}, S.h("td", { colspan: "8", class: "empty-cell",
                text: result.status === 404 ? "The SOS service is not installed on this build."
                                            : `Could not load the history (${result.detail}).` })));
            return;
        }
        const rows = requestsOf(result.data);
        const page = rows.slice(0, PAGE_SIZE);
        if (!page.length) {
            body.replaceChildren(S.h("tr", {}, S.h("td", { colspan: "8", class: "empty-cell",
                text: offset ? "No older requests." : "No assistance requests yet." })));
        } else {
            body.replaceChildren(...page.map(historyRow));
        }
        S.show(document.getElementById("sos-history-prev"), offset > 0);
        S.show(document.getElementById("sos-history-next"), rows.length > PAGE_SIZE);
        S.message(status, true, "");
    }

    // ── audit trail ─────────────────────────────────────────────────────────
    function eventRow(event) {
        const detail = event.detail && Object.keys(event.detail).length
            ? JSON.stringify(event.detail) : "";
        const states = event.from_state && event.from_state !== event.to_state
            ? `${event.from_state} → ${event.to_state}` : (event.to_state || "");
        return S.h("tr", {},
            S.h("td", { class: "mono", text: event.seq }),
            S.h("td", { class: "mono", text: S.localTime(event.at) }),
            S.h("td", { text: event.origin || "device" }),
            S.h("td", { text: event.event }),
            S.h("td", { class: "small", text: states }),
            S.h("td", { class: "small", text: [event.actor_type, (event.actor_id || "").slice(0, 24)]
                                               .filter(Boolean).join(" ") }),
            S.h("td", { class: "small", text: event.channel || "" }),
            S.h("td", { class: "audit-detail", text: detail.slice(0, 300) }));
    }

    async function loadAudit(requestId) {
        if (!requestId) return;
        selected = requestId;
        document.querySelectorAll("#sos-history-body tr").forEach(row => {
            row.classList.toggle("selected-row", row.dataset.request === requestId);
        });
        const card = document.getElementById("sos-audit-card");
        const body = document.getElementById("sos-audit-body");
        S.show(card, true);
        S.setText("sos-audit-id", "");
        body.replaceChildren(S.h("tr", {}, S.h("td", { colspan: "8", class: "empty-cell", text: "Loading…" })));
        const result = await S.api(`/api/sos/requests/${encodeURIComponent(requestId)}`);
        const chain = document.getElementById("sos-audit-chain");
        if (!result.ok) {
            body.replaceChildren(S.h("tr", {}, S.h("td", { colspan: "8", class: "empty-cell",
                text: result.status === 404 ? "Unknown request, or the SOS service is not installed."
                                            : `Could not load the audit trail (${result.detail}).` })));
            if (chain) chain.textContent = "";
            return;
        }
        const data = result.data || {};
        const request = data.request || data;
        const events = Array.isArray(data.events) ? data.events : [];
        S.setText("sos-audit-id", request.short_id || requestId.slice(0, 8));
        S.setText("sos-audit-state", [request.label || request.state,
                                      request.outcome ? `outcome: ${request.outcome}` : ""]
                                     .filter(Boolean).join(" · "));
        const status = data.chain || data.chain_status || null;
        if (chain) {
            chain.className = "chain-badge " + (status && status.ok ? "ok" : "bad");
            chain.textContent = !status ? "Chain status unavailable"
                : status.ok ? `Chain verified: ${status.entries || events.length} entries`
                : `Chain problems: ${(status.problems || []).slice(0, 3).join("; ") || "see the device log"}`;
        }
        body.replaceChildren(...(events.length ? events.map(eventRow)
            : [S.h("tr", {}, S.h("td", { colspan: "8", class: "empty-cell", text: "No events." }))]));
    }

    // ── preview and demo tools ──────────────────────────────────────────────
    function previewText(data) {
        if (typeof data === "string") return data;
        if (!data || typeof data !== "object") return "";
        const full = data.text || data.full || data.sos_text_full || data.preview || "";
        const sms = data.sms || data.sos_text_sms || "";
        return [full, sms ? `SMS version:\n${sms}` : ""].filter(Boolean).join("\n\n");
    }

    async function loadPreview() {
        const box = document.getElementById("sos-preview");
        if (!box) return;
        const result = await S.api("/api/sos/preview");
        if (result.ok) {
            box.textContent = previewText(result.data) || "No preview available.";
        } else {
            box.textContent = result.status === 404 ? "The SOS service is not installed on this build."
                : result.code === "unsupported" ? "The SOS service is off (SENTRIX_SOS_ENABLED=0)."
                : `Preview unavailable (${result.detail}).`;
        }
    }

    async function demoClose(button) {
        const status = document.getElementById("sos-history-message");
        button.disabled = true;
        const result = await S.api("/api/sos/demo/close-incident", { method: "POST" });
        button.disabled = false;
        S.message(status, result.ok, result.ok
            ? ((result.data && result.data.message) || "The incident will close after the next decision.")
            : S.sos.failureText(result));
    }

    async function demoTrigger(button) {
        const status = document.getElementById("sos-history-message");
        const select = document.getElementById("sos-demo-trigger-type");
        const kind = select ? select.value : "weapon";
        if (!window.confirm(`Open a SIMULATED ${kind} detection? Disclose it to the audience.`)) return;
        button.disabled = true;
        const result = await S.api("/api/sos/demo/trigger", {
            method: "POST", json: { incident_type: kind },
        });
        button.disabled = false;
        S.message(status, result.ok, result.ok
            ? ((result.data && result.data.message) || "SIMULATED DETECTION")
            : S.sos.failureText(result));
        if (result.ok) loadHistory();
    }

    async function helpNow(button) {
        const status = document.getElementById("sos-history-message");
        if (!window.confirm("Open a new assistance request from this one?")) return;
        button.disabled = true;
        const result = await S.sos.respond(button.dataset.helpNow, "help_now", {});
        button.disabled = false;
        S.message(status, result.ok, result.ok ? "A new assistance request was opened."
                                               : S.sos.failureText(result));
        if (result.ok) loadHistory();
    }

    document.addEventListener("DOMContentLoaded", () => {
        const page = document.getElementById("sos-page");
        selected = page ? page.dataset.selected || "" : "";
        S.onSos(renderCurrent);
        document.addEventListener("sentrix:sos-answered", () => {
            loadHistory();
            if (selected) loadAudit(selected);
        });
        document.getElementById("sos-history-body").addEventListener("click", event => {
            const target = event.target instanceof Element ? event.target : null;
            if (!target) return;
            const audit = target.closest("[data-audit-request]");
            if (audit) loadAudit(audit.dataset.auditRequest);
            const reopen = target.closest("[data-help-now]");
            if (reopen) helpNow(reopen);
        });
        document.getElementById("sos-history-prev").addEventListener("click", () => {
            offset = Math.max(0, offset - PAGE_SIZE);
            loadHistory();
        });
        document.getElementById("sos-history-next").addEventListener("click", () => {
            offset += PAGE_SIZE;
            loadHistory();
        });
        const demo = document.getElementById("sos-demo-close");
        if (demo) demo.addEventListener("click", () => demoClose(demo));
        const trigger = document.getElementById("sos-demo-trigger");
        if (trigger) trigger.addEventListener("click", () => demoTrigger(trigger));
        loadHistory();
        loadPreview();
        if (selected) loadAudit(selected);
    });
})();
