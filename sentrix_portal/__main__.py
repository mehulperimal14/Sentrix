"""``python -m sentrix_portal`` — run the Portal (default http://127.0.0.1:8100).

In development open it as http://portal.localhost:8100 so its cookie never
collides with the Local Console's on 127.0.0.1:8000 (decision: cookies
``portal_session`` vs ``lc_session``).
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from sentrix.paths import ROOT_DIR


def main() -> None:
    import uvicorn

    load_dotenv(ROOT_DIR / ".env")
    from sentrix_portal.app import create_app
    from sentrix_portal.settings import PortalSettings

    settings = PortalSettings.from_env()
    if settings.extras.get("ephemeral_secret"):
        print("PORTAL_SECRET_KEY is not set: using a temporary key "
              "(sessions end when the Portal restarts).")
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port,
                log_level=os.getenv("PORTAL_LOG_LEVEL", "warning"))


if __name__ == "__main__":
    main()
