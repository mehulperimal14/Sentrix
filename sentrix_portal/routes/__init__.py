"""Portal routers, included in this order by ``create_app``. Each module
exports ``router`` (and may export ``install(app)``, called after it is
included: the API's error model). The pages are the single-page app
(``routes/spa.py``, included last by ``create_app``, after ``/healthz``).
"""

ROUTER_MODULES: tuple[str, ...] = (
    "sentrix_portal.routes.link_hub",     # /link (device WebSocket)
    "sentrix_portal.routes.device_proxy", # /d/{id}/api|media|static, video, ws
    "sentrix_portal.routes.hooks",        # provider webhooks
    "sentrix_portal.routes.api_v2",       # the app's JSON API (docs/9)
)

__all__ = ["ROUTER_MODULES"]
