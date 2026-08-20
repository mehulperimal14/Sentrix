import os
import json
import time

EVAL_LOG_FILE = "evaluation/results/instrumentation.log"
_log_ready = False


def _is_eval_mode() -> bool:
    return os.getenv("SENTRIX_EVAL_MODE") == "1"


def init_eval_logger():
    """Call once at harness startup to clear any previous log."""
    global _log_ready
    if _is_eval_mode():
        os.makedirs("evaluation/results", exist_ok=True)
        with open(EVAL_LOG_FILE, "w") as f:
            pass
        _log_ready = True


def log_instrumentation(component: str, event_type: str, data: dict):
    """Write one JSON-lines entry. Safe to call at any time; no-ops outside EVAL_MODE."""
    if not _is_eval_mode():
        return
    try:
        os.makedirs("evaluation/results", exist_ok=True)
        log_entry = {
            "timestamp": time.time(),
            "component": component,
            "event_type": event_type,
            "data": data,
        }
        with open(EVAL_LOG_FILE, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
    except Exception:
        pass  # Never let instrumentation crash the pipeline

