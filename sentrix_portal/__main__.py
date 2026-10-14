"""``python -m sentrix_portal`` — run the Portal (default http://127.0.0.1:8100).

In development open it as http://portal.localhost:8100 so its cookie never
collides with the Local Console's on 127.0.0.1:8000 (decision: cookies
``portal_session`` vs ``lc_session``).
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from sentrix.paths import ROOT_DIR


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m sentrix_portal",
        description="SENTRIX Portal: accounts, onboarding, SOS, Desk and the device link hub.",
        epilog="Settings come from the environment (PORTAL_*; .env.example). Test mode "
               "(PORTAL_TEST_MODE) is on by default; background workers run only with "
               "PORTAL_WORKERS=1.")
    parser.add_argument("--host", default=None, help="bind address (PORTAL_HOST, default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="port (PORTAL_PORT, default 8100)")
    args = parser.parse_args(argv)

    import uvicorn

    load_dotenv(ROOT_DIR / ".env")
    from sentrix_portal.app import create_app
    from sentrix_portal.settings import PortalSettings

    overrides = {key: value for key, value in (("host", args.host), ("port", args.port))
                 if value is not None}
    settings = PortalSettings.from_env(**overrides)
    if settings.extras.get("key_files"):
        made = settings.extras.get("key_files_created") or []
        print(f"PORTAL_SECRET_KEY / PORTAL_DATA_KEY not set: using the key files in "
              f"{settings.data_dir}" + (f" (created {', '.join(made)})" if made else "")
              + ". Back them up: without them no device, login or code verifies.")
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port,
                log_level=os.getenv("PORTAL_LOG_LEVEL", "warning"))


if __name__ == "__main__":
    main()
