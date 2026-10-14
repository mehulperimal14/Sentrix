// static/js/live.js
// Live Feed tiles. Uses the shared, reconnecting websocket of app.js (the
// page used to open a second one that never reconnected).

"use strict";

(() => {
    const S = window.Sentrix;

    function update(data) {
        S.setText("ls-tci", ((Number(data.tci) || 0) * 100).toFixed(1) + "%");
        const alarm = data.alarm || { level: data.level, status: data.status,
                                      incident_type: data.incident_type };
        S.setText("ls-status", alarm.status || data.status || "SECURE");
        S.setText("ls-incident", alarm.level > 1 ? alarm.incident_type : data.incident_type);
        S.setText("ls-audio", data.audio_label || "—");
        S.setText("ls-rates", `${(data.video_fps || 0).toFixed(0)} fps / ${(data.ai_hz || 0).toFixed(1)} Hz`);
        const config = S.levels[alarm.level || data.level] || S.levels[1];
        const pill = S.setText("live-level-pill", config.label);
        if (pill) pill.style.color = config.color;
        // Through the Portal the video is relayed at its own rate.
        const remote = document.getElementById("remote-video-label");
        if (remote) {
            const rv = data.remote_video;
            S.show(remote, !!rv);
            if (rv) {
                remote.textContent = `remote ${Math.round(rv.fps || 0)} fps · relay delay ` +
                                     `${Math.round(rv.delay_ms || 0)} ms (the device shows ` +
                                     `${(data.video_fps || 0).toFixed(0)} fps)`;
            }
        }
    }

    document.addEventListener("DOMContentLoaded", () => S.onState(update));
})();
