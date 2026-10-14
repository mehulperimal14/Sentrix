// static/js/alerts.js
// Snapshot lightbox. The image URL comes from a data attribute the server
// built under the request's base; nothing is interpolated into a handler.

"use strict";

(() => {
    document.addEventListener("DOMContentLoaded", () => {
        const lightbox = document.getElementById("lightbox");
        const image = document.getElementById("lightbox-img");
        if (!lightbox || !image) return;
        document.querySelectorAll("[data-lightbox]").forEach(item => {
            item.addEventListener("click", () => {
                image.setAttribute("src", item.dataset.lightbox);
                lightbox.style.display = "flex";
            });
        });
        lightbox.addEventListener("click", () => {
            lightbox.style.display = "none";
        });
        document.addEventListener("keydown", event => {
            if (event.key === "Escape") lightbox.style.display = "none";
        });
    });
})();
