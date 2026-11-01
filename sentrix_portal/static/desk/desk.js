// SENTRIX Desk: queue polling and the alarm sound (Web Audio, no file).
// The alarm sounds while any request in the queue is not acknowledged; a
// browser plays sound only after a click, hence the "Enable" button.
(function () {
  "use strict";

  var audio = null;
  var beeping = null;

  function beep() {
    if (!audio) { return; }
    var osc = audio.createOscillator();
    var gain = audio.createGain();
    osc.type = "square";
    osc.frequency.value = 880;
    gain.gain.value = 0.08;
    osc.connect(gain);
    gain.connect(audio.destination);
    osc.start();
    osc.stop(audio.currentTime + 0.25);
  }

  function alarm(on) {
    if (on && !beeping) {
      beep();
      beeping = setInterval(beep, 1200);
    } else if (!on && beeping) {
      clearInterval(beeping);
      beeping = null;
    }
  }

  // The Portal's one clock ("1 Nov, 6:22 pm", IST), as the page renders it.
  var CLOCK = new Intl.DateTimeFormat("en-IN", { timeZone: "Asia/Kolkata", day: "numeric",
    month: "short", hour: "numeric", minute: "2-digit", hour12: true });
  function when(iso) {
    var t = Date.parse(iso);
    if (isNaN(t)) { return iso || ""; }
    var p = {};
    CLOCK.formatToParts(new Date(t)).forEach(function (x) { p[x.type] = x.value; });
    return p.day + " " + p.month + ", " + p.hour + ":" + p.minute + " " +
      String(p.dayPeriod || "").toLowerCase();
  }

  function cell(text) {
    var td = document.createElement("td");
    td.textContent = text == null ? "" : String(text);
    return td;
  }

  function render(table, requests) {
    var body = table.tBodies[0];
    body.textContent = "";
    requests.forEach(function (c) {
      var tr = document.createElement("tr");
      tr.className = "sos-state-" + String(c.state).toLowerCase() + (c.duress ? " duress" : "");
      var first = document.createElement("td");
      var link = document.createElement("a");
      link.className = "mono";
      link.href = "/desk/requests/" + encodeURIComponent(c.id);
      link.textContent = c.short_id;
      first.appendChild(link);
      if (c.duress) {
        var tag = document.createElement("strong");
        tag.className = "tag-duress";
        tag.textContent = " DURESS";
        first.appendChild(tag);
      }
      tr.appendChild(first);
      tr.appendChild(cell(c.label));
      tr.appendChild(cell(c.incident_type));
      tr.appendChild(cell(c.peak_level));
      tr.appendChild(cell([c.locality, c.city].filter(Boolean).join(" ")));
      tr.appendChild(cell(c.resident));
      tr.appendChild(cell(when(c.created_at)));
      body.appendChild(tr);
    });
    var empty = document.getElementById("desk-empty");
    if (empty) { empty.hidden = requests.length > 0; }
  }

  function poll(root, table, status) {
    fetch(root.dataset.queueUrl, { credentials: "same-origin",
                                   headers: { "Accept": "application/json",
                                              "X-Requested-With": "fetch" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) { status.textContent = "Queue unavailable"; return; }
        render(table, data.requests || []);
        var open = data.unacknowledged || 0;
        status.textContent = open ? open + " waiting for acknowledgement" : "All acknowledged";
        document.title = (open ? "(" + open + ") " : "") + "SENTRIX Desk";
        alarm(open > 0);
      })
      .catch(function () { status.textContent = "Queue unavailable"; });
  }

  // One request's page: reload when it moves on or leaves the queue (the
  // resident is safe, another operator resolved it, delivery failed).
  function watchCard(card) {
    var seen = false;
    setInterval(function () {
      fetch("/desk/api/queue", { credentials: "same-origin",
                                 headers: { "Accept": "application/json",
                                            "X-Requested-With": "fetch" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (!data) { return; }
          var mine = (data.requests || []).filter(function (c) {
            return c.id === card.dataset.request;
          })[0];
          // A closed request is not in the queue: reload once it leaves, never
          // over and over for one that was never there.
          if (mine) { seen = true; }
          if ((mine && mine.state !== card.dataset.state) || (!mine && seen)) {
            window.location.reload();
          }
        })
        .catch(function () {});
    }, 3000);
  }

  document.addEventListener("DOMContentLoaded", function () {
    var card = document.getElementById("desk-card");
    if (card) { watchCard(card); }
    var root = document.getElementById("desk");
    if (!root) { return; }
    var table = document.getElementById("desk-queue");
    var status = document.getElementById("desk-status");
    var button = document.getElementById("desk-sound");
    button.addEventListener("click", function () {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (Ctx && !audio) { audio = new Ctx(); }
      button.textContent = "Alarm sound on";
      button.disabled = true;
    });
    poll(root, table, status);
    setInterval(function () { poll(root, table, status); }, 3000);
  });
})();
