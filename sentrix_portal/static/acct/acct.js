// SENTRIX Portal (track T-ACCT): small progressive enhancements. Every page
// works without this file. Loaded as an external script with the request's
// CSP nonce; no inline handlers anywhere (data-* attributes instead).
(function () {
  "use strict";

  // <button data-print> prints the page (welcome letters, device labels).
  document.querySelectorAll("[data-print]").forEach(function (button) {
    button.addEventListener("click", function () { window.print(); });
  });

  // <button data-copy="#id"> copies the value or text of the target.
  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      var target = document.querySelector(button.getAttribute("data-copy"));
      if (!target) { return; }
      var text = target.value !== undefined ? target.value : target.textContent;
      var done = function () {
        var label = button.textContent;
        button.textContent = "Copied";
        window.setTimeout(function () { button.textContent = label; }, 1500);
      };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(done);
      } else if (target.select) {
        target.select();
        document.execCommand("copy");
        done();
      }
    });
  });

  // <form data-confirm="question"> asks before submitting.
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.getAttribute("data-confirm"))) { event.preventDefault(); }
    });
  });

  // <button data-geolocate> fills #f-lat / #f-lon from the browser (step 2).
  document.querySelectorAll("[data-geolocate]").forEach(function (button) {
    if (!navigator.geolocation) { button.hidden = true; return; }
    button.addEventListener("click", function () {
      var status = document.getElementById("geo-status");
      if (status) { status.textContent = "Locating..."; }
      navigator.geolocation.getCurrentPosition(function (position) {
        document.getElementById("f-lat").value = position.coords.latitude.toFixed(5);
        document.getElementById("f-lon").value = position.coords.longitude.toFixed(5);
        if (status) {
          status.textContent = "Location filled in (accuracy about " +
            Math.round(position.coords.accuracy) + " m). Check it on a map.";
        }
      }, function (error) {
        if (status) { status.textContent = "Could not get the location: " + error.message; }
      }, { enableHighAccuracy: true, timeout: 15000 });
    });
  });

  // A claim QR carries the code in the fragment (#code=KTRW-BQXL): it never
  // reaches the server's logs; fill it into the claim form.
  var claimCode = document.getElementById("f-claim_code");
  if (claimCode && !claimCode.value && window.location.hash.indexOf("#code=") === 0) {
    claimCode.value = decodeURIComponent(window.location.hash.slice(6)).slice(0, 16);
    if (window.history && window.history.replaceState) {
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
  }
})();
