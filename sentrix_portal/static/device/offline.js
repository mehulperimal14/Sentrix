// SENTRIX Portal: verdicts on the offline device page. They go to the same
// console route as when the device is online; the Portal queues them as a
// verdict.set command ("queued, will sync").
(function () {
  "use strict";

  document.addEventListener("DOMContentLoaded", function () {
    var root = document.getElementById("device-offline");
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (!root || !meta) { return; }
    var device = root.dataset.device;
    document.querySelectorAll(".js-verdict").forEach(function (button) {
      button.addEventListener("click", function () {
        var incident = button.dataset.incident;
        var status = document.querySelector('.js-verdict-status[data-incident="' +
                                            CSS.escape(incident) + '"]');
        var body = new URLSearchParams({ verdict: button.dataset.verdict });
        fetch("/d/" + encodeURIComponent(device) + "/api/incidents/" +
              encodeURIComponent(incident) + "/verdict", {
          method: "POST", credentials: "same-origin", body: body,
          headers: { "X-CSRF-Token": meta.content, "Accept": "application/json" }
        }).then(function (r) { return r.json().catch(function () { return {}; }); })
          .then(function (data) {
            if (status) { status.textContent = data.status || data.detail || "not saved"; }
          })
          .catch(function () { if (status) { status.textContent = "not saved"; } });
      });
    });
  });
})();
