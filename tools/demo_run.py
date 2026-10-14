"""Run the teachers' demo on one laptop: the Portal and the device together, in TEST MODE.

Run ``tools/demo_reset.py`` first. Then:

    python tools/demo_run.py                 # start both, print URLs and logins (Ctrl+C stops)
    python tools/demo_run.py scene gun       # play the held-out handgun clip once
    python tools/demo_run.py scene fire      # play the held-out flame clip once
    python tools/demo_run.py close           # test mode: close the open incident now
    python tools/demo_run.py trigger weapon  # test mode: disclosed SIMULATED DETECTION
    python tools/demo_run.py clips           # the scenes and their measured reliability

What ``start`` runs (docs/7 §1.2 laptop topology, design §6 "before the Pi"):

* **Portal** ``python -m sentrix_portal`` on ``http://portal.localhost:8100``
  (cookie ``portal_session``), workers on (hub watcher, cloud SOS timers,
  notification worker, retention), database and keys in the demo directory.
* **Device** ``python -m sentrix`` on ``http://127.0.0.1:8000`` (cookie
  ``lc_session``; a different host, so the two sessions never collide), with
  the SOS service, the channel router and the link client on, linked to the
  local Portal, ``SENTRIX_AUDIO=0`` (no microphone) and a **video file
  camera**: the demo reel (``sentrix/io/demo_reel.py``) loops normal footage and
  plays a trigger clip when ``scene <name>`` asks. Never a webcam, never wa02.
* Test mode on in both; ``SENTRIX_LIVE_ALERTS`` and every channel switch are
  forced **off** and provider keys blanked (``.env`` cannot bring them back:
  ``load_dotenv`` never overrides a set variable). The allowlist
  ``SENTRIX_TEST_RECIPIENTS`` is passed through from the environment only.
  Messages appear in the Portal's outbox, the Desk and the LC; nothing leaves
  the laptop. ``SENTRIX_SOS_DEMO_WINDOW_SECONDS`` (test mode only) gives the
  short demo window.

``close`` and ``trigger`` call the console's test-mode endpoints as the device
admin (``POST /api/sos/demo/close-incident`` and ``/api/sos/demo/trigger``),
which refuse outside test mode (``test_mode_only``). A scripted trigger is not a
detection: the SOS text and the audit say ``SIMULATED DETECTION``; disclose it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.append(str(TOOLS))

import demo_reset  # noqa: E402

VIDEOS = ROOT / "training" / "datasets" / "sentrix_custom" / "videos"
REEL_FILE = "reel.json"
CONTROL_FILE = "scene.txt"

#: Idle footage between scenes: vid07 0-60 s, labelled normal (0.92) from 0 to
#: 67 s (group of people talking; training split, used only as background).
#: M5 replay: 0 false onsets at every level on the normal footage.
IDLE = {"file": "7.mov", "start": 0.0, "end": 60.0, "split": "train"}

#: Trigger scenes, chosen by measured reliability (never wa02, never a clip used
#: to choose a threshold). Numbers: M5 replay through Pipeline.process
#: (training/runs/custom_eval/m5/replay_all.csv, commit 0b9e659, 6 Hz), time from
#: the clip start to the first committed L3 of the right type. The held-out
#: (val) clips are the demo's main scenes; the in-sample backups are disclosed as
#: such. Live reliability (10 pre-runs per clip, keep >= 9/10; reviews) is
#: measured on demo-day hardware, not here.
SCENES: dict[str, dict[str, Any]] = {
    "gun": {"file": "gun.mov", "class": "weapon", "split": "val", "held_out": True,
            "end": None, "l3_seconds": 7.3, "peak_level": 3,
            "note": "replica handgun from 1.0 s; held-out handgun onset->L3 up to "
                    "6.3 s after the M1 floors"},
    "fire": {"file": "4.mov", "class": "fire", "split": "val", "held_out": True,
             "end": None, "l3_seconds": 10.9, "peak_level": 3,
             "note": "aerosol flame jet 10.6-13.8 s"},
    "gun-b": {"file": "2.mov", "class": "weapon", "split": "train", "held_out": False,
              "end": None, "l3_seconds": 10.9, "peak_level": 5,
              "note": "backup, IN-SAMPLE (training clip): corroborated, reaches L5"},
    "fire-b": {"file": "6.mov", "class": "fire", "split": "train", "held_out": False,
               "end": None, "l3_seconds": 4.8, "peak_level": 5,
               "note": "backup, IN-SAMPLE (training clip): corroborated, reaches L5"},
}

#: Forced in both processes (on top of demo_reset.process_env).
FORCED_OFF = {
    "SENTRIX_LIVE_ALERTS": "0", "SENTRIX_MODEM_LIVE": "0", "SENTRIX_TWILIO_ENABLED": "0",
    "SENTRIX_TELEGRAM_LIVE": "0", "SENTRIX_EMAIL_LIVE": "0", "SENTRIX_WEBPUSH_LIVE": "0",
    "SENTRIX_MODEM_ENABLED": "0", "SENTRIX_GPIO_ENABLED": "0",
    # provider credentials and legacy personal fields: empty, so .env cannot supply them
    "TWILIO_ACCOUNT_SID": "", "TWILIO_AUTH_TOKEN": "", "TWILIO_PHONE_NUMBER": "",
    "ALERT_PHONE_NUMBER": "", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_WEBHOOK_SECRET": "",
    "BREVO_API_KEY": "", "RESEND_API_KEY": "", "SENTRIX_USER_NAME": "",
    "SENTRIX_USER_ADDRESS": "", "SENTRIX_USER_PHONE": "",
    "NOTIFY_SOCKET": "", "WATCHDOG_USEC": "",
}
ON = "1"


# ── the reel ─────────────────────────────────────────────────────────────────
def write_reel(directory: Path) -> Path:
    """``<demo>/reel.json`` for ``CAMERA_SOURCES=demo-reel:<path>``."""
    from sentrix.io.demo_reel import refuse_test_video
    scenes = {}
    for name, scene in SCENES.items():
        path = VIDEOS / scene["file"]
        refuse_test_video(path)
        scenes[name] = {"file": str(path), "start": 0.0, "end": scene["end"]}
    idle = VIDEOS / IDLE["file"]
    refuse_test_video(idle)
    reel = {"control": str(Path(directory) / CONTROL_FILE),
            "idle": {"file": str(idle), "start": IDLE["start"], "end": IDLE["end"]},
            "scenes": scenes}
    path = Path(directory) / REEL_FILE
    path.write_text(json.dumps(reel, indent=2) + "\n", encoding="utf-8")
    return path


# ── environments ─────────────────────────────────────────────────────────────
def _port(url: str, default: int) -> int:
    return urlsplit(url).port or default


def common_env(demo: dict[str, Any], base: dict[str, str] | None = None) -> dict[str, str]:
    base = dict(os.environ if base is None else base)
    env = {**base, **demo["env"], **FORCED_OFF,
           "SENTRIX_TEST_MODE": ON, "PORTAL_TEST_MODE": ON,
           # the allowlist comes from the environment only (never .env, never a file)
           "SENTRIX_TEST_RECIPIENTS": base.get("SENTRIX_TEST_RECIPIENTS", ""),
           "SENTRIX_SOS_DEMO_WINDOW_SECONDS": str(int(demo["window_seconds"])),
           "PYTHONUNBUFFERED": "1"}
    desk = base.get("SENTRIX_DESK_NUMBER", "") or base.get("PORTAL_DESK_NUMBER", "")
    env["SENTRIX_DESK_NUMBER"] = env["PORTAL_DESK_NUMBER"] = desk
    return env


def portal_env(demo: dict[str, Any], base: dict[str, str] | None = None) -> dict[str, str]:
    env = common_env(demo, base)
    port = _port(demo["portal_url"], 8100)
    env.update({"PORTAL_HOST": "127.0.0.1", "PORTAL_PORT": str(port),
                "PORTAL_BASE_URL": demo["portal_url"], "PORTAL_WORKERS": ON,
                "PORTAL_SECURE_COOKIES": "0", "SENTRIX_TELEGRAM_MODE": "polling",
                # the Portal process builds no device services
                "SENTRIX_SOS_ENABLED": "0", "SENTRIX_LINK_ENABLED": "0",
                "SENTRIX_CHANNELS_ENABLED": "0", "SENTRIX_NODE_ENABLED": "0",
                "SENTRIX_AUDIO": "0", "CAMERA_SOURCES": ""})
    return env


def device_env(demo: dict[str, Any], reel: Path,
               base: dict[str, str] | None = None) -> dict[str, str]:
    env = common_env(demo, base)
    secrets_ = demo["device_secrets"]
    lc_port = _port(demo["lc_url"], 8000)
    portal_port = _port(demo["portal_url"], 8100)
    env.update({
        "CAMERA_SOURCES": f"demo-reel:{reel}", "SENTRIX_AUDIO": "0",
        "SENTRIX_HOST": "127.0.0.1", "SENTRIX_PORT": str(lc_port),
        "PUBLIC_SERVER_URL": demo["lc_url"], "SENTRIX_SECURE_COOKIES": "0",
        "SENTRIX_PASSWORD": secrets_["lc_password"],
        "SESSION_SECRET": secrets_["session_secret"],
        "EVIDENCE_AES_KEY": secrets_["evidence_key"],
        "SENTRIX_SOS_ENABLED": ON, "SENTRIX_CHANNELS_ENABLED": ON, "SENTRIX_LINK_ENABLED": ON,
        "SENTRIX_NODE_ENABLED": ON, "SENTRIX_PROFILE": "laptop", "SENTRIX_ARMED": "0",
        # The link goes to the loopback address (Windows does not resolve
        # *.localhost for Python); browsers use portal.localhost.
        "SENTRIX_PORTAL_URL": f"http://127.0.0.1:{portal_port}",
        "SENTRIX_SIREN": "console",
    })
    return env


def assert_safe(env: dict[str, str]) -> None:
    """The invariants every demo process starts with (also unit-tested)."""
    if env.get("SENTRIX_TEST_MODE") != ON or env.get("PORTAL_TEST_MODE") != ON:
        raise RuntimeError("demo processes run in test mode only")
    for key in ("SENTRIX_LIVE_ALERTS", "SENTRIX_MODEM_LIVE", "SENTRIX_TWILIO_ENABLED",
                "SENTRIX_TELEGRAM_LIVE", "SENTRIX_EMAIL_LIVE", "SENTRIX_WEBPUSH_LIVE"):
        if env.get(key) != "0":
            raise RuntimeError(f"{key} must be 0 in a demo")
    camera = env.get("CAMERA_SOURCES", "")
    if any(part.strip().isdigit() or "://" in part for part in camera.split(",")):
        raise RuntimeError("a demo never opens a webcam or a stream")
    if env.get("SENTRIX_AUDIO", "0") != "0":
        raise RuntimeError("a demo never opens the microphone")
    real_var = (ROOT / "var").resolve()
    var_dir = Path(env.get("SENTRIX_VAR_DIR") or real_var).resolve()
    if var_dir == real_var:
        raise RuntimeError("a demo never uses the real var/ (var/sentrix.db)")


# ── processes ────────────────────────────────────────────────────────────────
def _spawn(args: list[str], env: dict[str, str], log_path: Path) -> subprocess.Popen:
    assert_safe(env)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_path, "ab")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen(args, cwd=str(ROOT), env=env, stdout=handle,
                            stderr=subprocess.STDOUT, creationflags=flags)


def _wait_http(url: str, process: subprocess.Popen, seconds: float) -> bool:
    import httpx
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            if httpx.get(url, timeout=2.0).status_code < 500:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


def _stop(process: subprocess.Popen, name: str) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.send_signal(signal.SIGINT)
        process.wait(timeout=20)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        print(f"{name} did not stop in 20 s: killing it", file=sys.stderr)
        process.kill()
        process.wait(timeout=10)


def banner(demo: dict[str, Any]) -> str:
    users = demo["usernames"]
    portal, lc = demo["portal_url"], demo["lc_url"]
    lines = [
        "SENTRIX demo running - TEST MODE, nothing is sent (live switches forced off)",
        "",
        f"Portal (projector / phone)    {portal}/login",
        f"  company admin               {users['admin']}  (password + TOTP from demo_reset); "
        f"console {portal}/admin",
        f"  Desk console                {portal}/desk   as {users['operator']} (TOTP)",
        f"  customer                    {users['owner']}  ({demo['customer_name']})",
        f"Local Console (laptop A)      {lc}/login   device admin password "
        f"{demo['device_secrets']['lc_password']}",
        f"  same console via the tunnel {portal}/d/{demo['device_id']}/dashboard",
        f"  SOS page (close / scripted)  {lc}/sos",
        "",
        f"Demo window {demo['window_seconds']} s; safety net "
        f"{'on' if demo['safety_net'] else 'off'}; live wizard steps "
        f"{', '.join(map(str, demo['live_steps'])) or 'none'}"
        + (f" (claim code on the label {demo['paths']['label']})"
           if 5 in demo["live_steps"] else ""),
        "",
        "Scenes (another terminal):",
    ]
    for name, scene in SCENES.items():
        lines.append(f"  python tools/demo_run.py scene {name:<7} {scene['file']:<8} "
                     f"{scene['class']:<6} L3 ~{scene['l3_seconds']:.0f} s"
                     f"{'' if scene['held_out'] else '  (in-sample backup)'}")
    lines += ["  python tools/demo_run.py close            close the incident before the next scene",
              "  python tools/demo_run.py trigger weapon   DISCLOSED scripted trigger "
              "(SIMULATED DETECTION)",
              "", f"Logs: {demo['paths']['logs']}", "Ctrl+C stops both."]
    return "\n".join(lines)


def start(directory: Path) -> int:
    demo = demo_reset.load_demo(directory)
    reel = write_reel(directory)
    logs = Path(demo["paths"]["logs"])
    python = sys.executable
    portal = _spawn([python, "-m", "sentrix_portal"], portal_env(demo), logs / "portal.log")
    device = None
    try:
        if not _wait_http(f"http://127.0.0.1:{_port(demo['portal_url'], 8100)}/healthz",
                          portal, 60):
            print(f"the Portal did not start: see {logs / 'portal.log'}", file=sys.stderr)
            return 1
        device = _spawn([python, "-m", "sentrix"], device_env(demo, reel),
                        logs / "device.log")
        if not _wait_http(f"{demo['lc_url']}/login", device, 240):
            print(f"the device did not start: see {logs / 'device.log'}", file=sys.stderr)
            return 1
        print(banner(demo), flush=True)
        while portal.poll() is None and device.poll() is None:
            time.sleep(1.0)
        print("a demo process exited: stopping the other one (see the logs)", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("stopping...", flush=True)
        return 0
    finally:
        if device is not None:
            _stop(device, "device")
        _stop(portal, "Portal")


# ── operator commands ────────────────────────────────────────────────────────
def scene(directory: Path, name: str) -> int:
    from sentrix.io.demo_reel import write_control
    demo_reset.load_demo(directory)
    if name != "idle" and name not in SCENES:
        print(f"unknown scene {name!r}; one of: idle, {', '.join(SCENES)}", file=sys.stderr)
        return 2
    write_control(Path(directory) / CONTROL_FILE, name)
    if name == "idle":
        print("back to the idle footage")
    else:
        info = SCENES[name]
        print(f"scene {name}: {info['file']} ({info['class']}), committed L3 expected after "
              f"~{info['l3_seconds']:.0f} s (M5 replay); {info['note']}")
    return 0


_CSRF = re.compile(r'<meta name="csrf-token" content="([^"]*)"')


def _lc_client(demo: dict[str, Any]):
    """An httpx client signed in to the demo console as the device admin."""
    import httpx
    client = httpx.Client(base_url=demo["lc_url"], timeout=20.0, follow_redirects=False)
    response = client.post("/login", data={"password": demo["device_secrets"]["lc_password"]})
    if response.status_code not in (302, 303):
        raise RuntimeError(f"console login failed ({response.status_code})")
    page = client.get("/sos")
    found = _CSRF.search(page.text)
    if not found or not found.group(1):
        raise RuntimeError("no CSRF token on the console's SOS page")
    client.headers.update({"X-CSRF-Token": found.group(1), "X-Requested-With": "fetch",
                           "Accept": "application/json"})
    return client


def _post(directory: Path, path: str, body: dict | None) -> int:
    import httpx
    demo = demo_reset.load_demo(directory)
    client = None
    try:
        client = _lc_client(demo)
        response = client.post(path, json=body or {})
    except (httpx.HTTPError, RuntimeError) as exc:
        print(f"the console is not reachable: {exc}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()
    data = response.json() if "json" in response.headers.get("content-type", "") else {}
    print(f"{response.status_code} {data.get('code', '')} {data.get('message', '')}".strip())
    return 0 if response.status_code == 200 else 1


def close(directory: Path) -> int:
    return _post(directory, "/api/sos/demo/close-incident", None)


def trigger(directory: Path, kind: str) -> int:
    print("SCRIPTED TRIGGER: this is a SIMULATED DETECTION, not a model result. "
          "Tell the audience.", flush=True)
    return _post(directory, "/api/sos/demo/trigger", {"incident_type": kind})


def clips() -> int:
    print(f"idle: {IDLE['file']} {IDLE['start']:.0f}-{IDLE['end']:.0f} s "
          f"({IDLE['split']}, normal footage)")
    for name, info in SCENES.items():
        print(f"{name:<7} {info['file']:<8} {info['class']:<6} {info['split']:<5} "
              f"L3 at {info['l3_seconds']:>4.1f} s, peak L{info['peak_level']}  {info['note']}")
    print("Source: M5 replay, training/runs/custom_eval/m5/replay_all.csv (commit 0b9e659, "
          "6 Hz). wa02 is never used. Rehearse each clip 10 times on the demo laptop and "
          "keep only clips that reach L3 at least 9 times.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = ("start", "scene", "close", "trigger", "clips")
    if not any(token in commands for token in argv) and not {"-h", "--help"} & set(argv):
        argv.append("start")                          # the default command
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default=str(demo_reset.DEFAULT_DIR),
                        help="demo directory made by tools/demo_reset.py (default var/demo)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in commands:
        command = sub.add_parser(name)
        command.add_argument("--dir", default=argparse.SUPPRESS,
                             help="demo directory (may also come before the command)")
        if name == "scene":
            command.add_argument("name", help=f"idle, {', '.join(SCENES)}")
        if name == "trigger":
            command.add_argument("kind", nargs="?", default="weapon",
                                 choices=("weapon", "fire", "intrusion"))
    args = parser.parse_args(argv)
    directory = Path(args.dir)
    if "sentrix" not in sys.modules:
        # This process imports sentrix too (the reel helpers): keep it off var/.
        os.environ["SENTRIX_VAR_DIR"] = str(demo_reset.layout(directory)["device"])
    if args.command == "clips":
        return clips()
    try:
        if args.command == "start":
            return start(directory)
        if args.command == "scene":
            return scene(directory, args.name)
        if args.command == "close":
            return close(directory)
        return trigger(directory, args.kind)
    except demo_reset.DemoRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
