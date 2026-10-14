// SENTRIX Portal: SOS countdowns, request polling and the global
// "Do you need assistance?" banner. External file (CSP nonce), DOM built with
// textContent only: request data comes from devices and is never HTML.
(function () {
  "use strict";

  function pad(n) { return n < 10 ? "0" + n : String(n); }

  function tickCountdowns() {
    var now = Date.now();
    document.querySelectorAll(".countdown[data-expires]").forEach(function (el) {
      var expires = Date.parse(el.dataset.expires);
      if (isNaN(expires)) { return; }
      var left = Math.max(0, Math.round((expires - now) / 1000));
      el.textContent = left > 0 ? Math.floor(left / 60) + ":" + pad(left % 60)
                                : "lapsing now";
    });
  }

  function getJSON(url) {
    return fetch(url, { credentials: "same-origin",
                        headers: { "Accept": "application/json",
                                   "X-Requested-With": "fetch" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }

  // One request page: reload when the request moves on (the server renders
  // the buttons the new state allows).
  function watchRequest(root) {
    var id = root.dataset.request;
    var state = root.dataset.state;
    setInterval(function () {
      getJSON("/sos/api/requests/" + encodeURIComponent(id)).then(function (data) {
        if (data && data.request && data.request.state !== state) {
          window.location.reload();
        }
      });
    }, 3000);
  }

  // Any Portal page that includes this script: a banner while a request of
  // the household waits for an answer.
  function banner() {
    var bar = null;
    var here = (document.getElementById("sos-request") || {}).dataset;
    function show(requests) {
      var waiting = requests.filter(function (r) {
        return r.state === "PENDING" || r.state === "DRAFTED";
      }).filter(function (r) { return !here || here.request !== r.id; });
      if (!waiting.length) {
        if (bar) { bar.remove(); bar = null; }
        return;
      }
      var r = waiting[0];
      if (!bar) {
        bar = document.createElement("div");
        bar.className = "sos-banner";
        bar.setAttribute("role", "alert");
        document.body.insertBefore(bar, document.body.firstChild);
      }
      bar.textContent = "";
      var text = document.createElement("span");
      text.textContent = "Do you need assistance? " + r.short_id + ": " + r.label +
        " (" + r.incident_type + ")";
      var link = document.createElement("a");
      link.href = "/sos/" + encodeURIComponent(r.id);
      link.textContent = "Answer now";
      bar.appendChild(text);
      bar.appendChild(link);
    }
    function poll() {
      getJSON("/sos/api/open").then(function (data) {
        if (data && Array.isArray(data.requests)) { show(data.requests); }
      });
    }
    poll();
    setInterval(poll, 5000);
  }

  document.addEventListener("DOMContentLoaded", function () {
    tickCountdowns();
    setInterval(tickCountdowns, 1000);
    var root = document.getElementById("sos-request");
    if (root) { watchRequest(root); }
    if (document.querySelector('meta[name="csrf-token"]')) { banner(); }
  });
})();
