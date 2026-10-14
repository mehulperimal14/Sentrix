// static/js/setup.js
// /setup/wifi (physical setup mode only): find networks and join one. The
// setup code from the device label unlocks both calls; nothing is stored in
// the browser. Network names are shown with textContent.

"use strict";

(() => {
    const base = document.body.dataset.base || "";
    const meta = document.querySelector('meta[name="csrf-token"]');
    const csrf = meta ? (meta.getAttribute("content") || "") : "";

    function url(path) {
        return base + path;
    }

    function say(text, ok) {
        const box = document.getElementById("setup-message");
        if (!box) return;
        box.textContent = text;
        box.classList.toggle("ok", !!ok);
        box.classList.toggle("bad", !ok);
    }

    function showStatus(status) {
        const node = document.getElementById("setup-status");
        if (!node || !status) return;
        node.textContent = status.connected
            ? `Connected to ${status.ssid || "a network"}${status.ip ? " (" + status.ip + ")" : ""}`
            : "Not connected to Wi-Fi yet.";
    }

    async function post(target, payload) {
        let res;
        try {
            res = await fetch(target, {
                method: "POST", credentials: "same-origin",
                headers: { "Content-Type": "application/json", "Accept": "application/json",
                           "X-CSRF-Token": csrf, "X-Requested-With": "fetch" },
                body: JSON.stringify(payload),
            });
        } catch (err) {
            return { ok: false, data: { detail: "The unit cannot be reached." } };
        }
        let data = {};
        try { data = await res.json(); } catch (err) { data = {}; }
        return { ok: res.ok, data };
    }

    function code() {
        const input = document.getElementById("setup-code");
        return input ? input.value.trim() : "";
    }

    async function scan() {
        say("Looking for networks…", true);
        const result = await post(url("/setup/wifi/scan"), { code: code() });
        if (!result.ok) {
            say(result.data.detail || result.data.error || "Scan failed.", false);
            return;
        }
        const list = document.getElementById("setup-networks");
        const networks = Array.isArray(result.data.networks) ? result.data.networks : [];
        list.replaceChildren(...networks.map(network => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "link-btn";
            button.textContent = `${network.ssid}  (${network.signal ?? "?"}%, ${network.security || "open"})`;
            button.addEventListener("click", () => {
                document.getElementById("setup-ssid").value = network.ssid;
                document.getElementById("setup-psk").focus();
            });
            const item = document.createElement("li");
            item.append(button);
            return item;
        }));
        say(networks.length ? "Choose a network." : "No networks found; move the unit closer to the router.", true);
        showStatus(result.data.status);
    }

    async function connect() {
        const ssid = document.getElementById("setup-ssid").value.trim();
        const psk = document.getElementById("setup-psk").value;
        if (!ssid) {
            say("Choose a network first.", false);
            return;
        }
        say("Connecting…", true);
        const result = await post(url("/setup/wifi"), { code: code(), ssid, psk });
        document.getElementById("setup-psk").value = "";
        if (result.ok && result.data.ok) {
            say("Connected. The unit leaves setup mode when you press its setup button again.", true);
        } else {
            say(result.data.error || result.data.detail || "Could not connect.", false);
        }
        showStatus(result.data.status);
    }

    document.addEventListener("DOMContentLoaded", () => {
        document.getElementById("setup-scan").addEventListener("click", scan);
        document.getElementById("setup-connect").addEventListener("click", connect);
    });
})();
