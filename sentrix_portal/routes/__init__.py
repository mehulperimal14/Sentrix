"""Portal routers, one module per area, included in this order by
``create_app``. Owners: docs/7 §2. Each module exports ``router``.
"""

ROUTER_MODULES: tuple[str, ...] = (
    # T-ACCT: accounts, onboarding, household, settings, privacy, home, admin
    "sentrix_portal.routes.auth",
    "sentrix_portal.routes.home",
    "sentrix_portal.routes.onboarding",
    "sentrix_portal.routes.household",
    "sentrix_portal.routes.settings",
    "sentrix_portal.routes.privacy",
    "sentrix_portal.routes.admin",
    # T-LINK: device link, tunnel parity, SOS, Desk, contact pages
    "sentrix_portal.routes.link_hub",
    "sentrix_portal.routes.device_proxy",
    "sentrix_portal.routes.sos",
    "sentrix_portal.routes.desk",
    "sentrix_portal.routes.contacts",
    # T-CH: provider webhooks
    "sentrix_portal.routes.hooks",
)

__all__ = ["ROUTER_MODULES"]
