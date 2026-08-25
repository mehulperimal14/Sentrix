# core/engine_instance.py
#
# ARCHITECTURE: Module-level singleton for the SystemEngine and related engines.
# Imported by app.py and web routes. initialize_all() is called once at startup
# via the FastAPI lifespan handler. Separates construction from import time so
# that importing this module does not trigger heavy model loading.

from core.system_engine import SystemEngine

_system_engine: SystemEngine = None


def initialize_all():
    """Initialise all engines. Called once from app.py lifespan startup."""
    global _system_engine
    _system_engine = SystemEngine()
    print("[EngineInstance] All engines initialised.")


def get_system_engine() -> SystemEngine:
    """Return the shared SystemEngine instance."""
    global _system_engine
    if _system_engine is None:
        _system_engine = SystemEngine()
    return _system_engine


# Legacy alias used by old web routes / websocket handler
class _LegacyEngineProxy:
    """Thin proxy so old code that does `engine.get_live_metrics()` still works."""
    def get_live_metrics(self):
        return get_system_engine().get_live_metrics()

engine = _LegacyEngineProxy()