"""Pi 5 header I/O on gpiozero: status LEDs, buzzer and the sensor inputs.

**RPi.GPIO does not work on a Raspberry Pi 5** (the header sits behind the
RP1 south bridge); gpiozero drives it through lgpio (docs/8_RASPBERRY_PI5_INTEGRATION.md §3). Off a Pi
every device runs on gpiozero's ``MockFactory`` pretending to be a Pi 5, so
the same code runs on a laptop and in the tests, and reports itself as not
real.

Pins (roadmap v2; ``settings.gpio``):

======  =========================  ===========================================
GPIO    Device                     Meaning
======  =========================  ===========================================
17      siren relay (NPN driver)   ``sentrix.io.actuators.GpioSiren``, not here
5/12/13 green / amber / red LED    see :func:`led_patterns`
26      active buzzer (NPN)        chirps, see :func:`buzzer_events`
23      PIR (HC-SR50x, 3.3 V out)  motion: pin high
24      door reed to GND, pull-up  contact closed = door closed
16      PC817 mains sense, pull-up output low while mains is present
opt.    siren arming key, pull-up  closed to GND = siren line armed
opt.    enclosure lid, pull-up     closed to GND = lid shut (open = tamper)
======  =========================  ===========================================

The service (``build_service``, flag ``SENTRIX_GPIO_ENABLED``) owns these
devices. One thread polls the live state five times a second and turns it
into LED patterns and buzzer chirps; inputs arrive on gpiozero's callbacks,
are debounced in software (a level must hold for ``input_bounce_seconds``),
and are re-checked by the same poll in case an edge was missed. Accepted
input changes update the ``node`` state block, are written to the session
event log (``node_input`` / ``node_alert`` audit rows) and, for mains loss,
mains return and tamper, are published to the Portal as ``node.alert``
(docs/7 §5.5).

Pin factory (``SENTRIX_GPIO_PIN_FACTORY``): ``""`` auto = lgpio on a
Raspberry Pi, mock elsewhere; ``lgpio``; ``mock``. One factory instance per
process (:func:`shared_pin_factory`) serves the siren relay and this service.
A Pi whose lgpio does not load (``python3-lgpio`` missing, or a venv made
without ``--system-site-packages``) gets mock pins *and* an error that
``/api/health``, the Node page and ``doctor`` show — never silent.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sentrix.io import sensors, tunables
from sentrix.utils.logging import get_logger

log = get_logger("gpio")

#: MockFactory board revision: a Raspberry Pi 5 Model B, 8 GB.
MOCK_REVISION = "d04170"

# LED patterns, from least to most urgent.
OFF, SLOW, FAST, SOLID = "off", "slow", "fast", "solid"
PATTERN_ORDER = (OFF, SLOW, FAST, SOLID)
#: (on seconds, off seconds) of the blinking patterns.
BLINK_TIMES = {SLOW: (1.0, 1.0), FAST: (0.2, 0.2)}

#: Buzzer patterns: (on seconds, off seconds, repeats).
CHIRP_SHORT = (0.08, 0.12)
CHIRP_LONG = (0.5, 0.3)
#: Committed level -> short chirps when the level rises to it. L5 is the siren.
LEVEL_CHIRPS = {2: 1, 3: 2, 4: 3}
#: A PIR motion row in the event log at most this often (a busy room).
PIR_LOG_INTERVAL_SECONDS = 30.0

_OPEN_SOS_STATES = ("DRAFTED", "PENDING", "APPROVED", "DISPATCHED", "ACKNOWLEDGED", "FAILED")


# ── pin factory ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PinFactoryInfo:
    factory: Any               # a gpiozero Factory, or None without gpiozero
    kind: str                  # lgpio | mock | none
    real: bool                 # drives real pins
    error: str | None = None   # why a requested real factory is not in use


def _mock_factory() -> Any:
    from gpiozero.pins.mock import MockFactory
    return MockFactory(revision=MOCK_REVISION)


def resolve_pin_factory(name: str | None = None, *,
                        on_pi: bool | None = None) -> PinFactoryInfo:
    """Build the pin factory ``name`` asks for ("" auto, "lgpio", "mock").

    Never raises. A real factory that cannot load falls back to mock pins and
    says why in ``error``; without gpiozero the factory is None.
    """
    from sentrix.config import settings
    name = (settings.gpio.pin_factory if name is None else name).strip().lower()
    try:
        import gpiozero  # noqa: F401
    except Exception as exc:                        # pragma: no cover - in requirements
        return PinFactoryInfo(None, "none", False, f"gpiozero not installed: {exc}")
    if name not in ("", "auto", "lgpio", "mock"):
        return PinFactoryInfo(_mock_factory(), "mock", False,
                              f"unknown SENTRIX_GPIO_PIN_FACTORY {name!r} (use lgpio or mock)")
    if name == "mock":
        return PinFactoryInfo(_mock_factory(), "mock", False)
    on_pi = sensors.is_raspberry_pi() if on_pi is None else on_pi
    if name in ("", "auto") and not on_pi:
        return PinFactoryInfo(_mock_factory(), "mock", False)
    try:
        from gpiozero.pins.lgpio import LGPIOFactory
        return PinFactoryInfo(LGPIOFactory(), "lgpio", True)
    except Exception as exc:
        error = (f"lgpio unavailable ({exc}); install python3-lgpio and create the "
                 f"venv with --system-site-packages")
        log.error("GPIO: %s — using mock pins, the siren and LEDs are NOT connected", error)
        return PinFactoryInfo(_mock_factory(), "mock", False, error)


_factory_lock = threading.Lock()
_shared: PinFactoryInfo | None = None


def shared_pin_factory() -> PinFactoryInfo:
    """The process-wide factory (resolved once from ``settings.gpio``)."""
    global _shared
    with _factory_lock:
        if _shared is None:
            _shared = resolve_pin_factory()
            log.info("GPIO pin factory: %s%s", _shared.kind,
                     "" if _shared.real else " (no hardware)")
        return _shared


def _reset_shared_pin_factory() -> None:
    """Tests only."""
    global _shared
    with _factory_lock:
        _shared = None


# ── debounced inputs ──────────────────────────────────────────────────────────
class Debouncer:
    """Accept a level only after it held for ``bounce`` seconds.

    ``poke()`` on every raw edge (re)starts the settle timer; when it fires
    the input is read again and, if it differs from the accepted value,
    ``on_change(value)`` runs. ``poll()`` (called periodically) pokes when the
    input disagrees with the accepted value and no timer is pending, so a
    missed edge is caught within one poll. ``bounce <= 0`` settles at once.
    ``timer_factory(delay, fn)`` returns an object with ``start``/``cancel``
    (``threading.Timer`` by default; tests pass a manual one).
    """

    def __init__(self, read: Callable[[], Any], on_change: Callable[[Any], None],
                 bounce: float, *, timer_factory: Callable[..., Any] | None = None) -> None:
        self._read = read
        self._on_change = on_change
        self.bounce = max(0.0, float(bounce))
        self._timer_factory = timer_factory or _daemon_timer
        self._lock = threading.Lock()
        self._timer: Any = None
        self.value = read()

    def poke(self) -> None:
        if self.bounce <= 0:
            self._settle()
            return
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = self._timer_factory(self.bounce, self._settle)
            self._timer.start()

    def poll(self) -> None:
        with self._lock:
            pending = self._timer is not None
        if not pending and self._read() != self.value:
            self.poke()

    def _settle(self) -> None:
        value = self._read()
        with self._lock:
            self._timer = None
            if value == self.value:
                return
            self.value = value
        self._on_change(value)

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


def _daemon_timer(delay: float, fn: Callable[[], None]) -> threading.Timer:
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    return timer


@dataclass(frozen=True)
class InputSpec:
    """One input: ``value(is_active)`` maps gpiozero's active state (low when
    pulled up, high when pulled down) to the reported value."""

    name: str
    pin: int
    pull_up: bool
    value: Callable[[bool], Any]


def forward_sensor_event(kind: str, **detail: Any) -> bool:
    """A PIR trigger or a door change reaches the pipeline as presence
    context (``Pipeline.note_sensor``: the TCI's unrecognised-person rule,
    the event log, the dashboard's contributions - never a threat alone, D15)
    and opens the clip engine's S0 gate (``ClipBuffer.add_event``), both on
    the frames' monotonic clock. Only an already-running pipeline is used:
    this never builds one (which would open the camera). Returns whether an
    event was delivered."""
    engine = sys.modules.get("sentrix.pipeline.engine")
    pipeline = getattr(engine, "_pipeline", None) if engine is not None else None
    if pipeline is None:
        return False
    at = time.monotonic()
    clean = {k: v for k, v in detail.items() if v is not None}
    delivered = False
    note = getattr(pipeline, "note_sensor", None)
    try:
        if callable(note):
            note(kind, at, **clean)
            delivered = True
        strategy = getattr(pipeline, "_strategy", None)
        buffer = getattr(getattr(strategy, "runner", None), "buffer", None)
        if buffer is not None and hasattr(buffer, "add_event"):
            buffer.add_event(kind, at, **clean)
            delivered = True
    except Exception as exc:                        # the GPIO thread must not die
        log.debug("sensor event %s not delivered: %s", kind, exc)
    return delivered


def input_specs(cfg: Any) -> list[InputSpec]:
    """Every configured input. ``cfg`` is ``settings.gpio``."""
    specs = [
        InputSpec("pir", cfg.pir_pin, False, lambda active: bool(active)),
        # Contact closed (magnet near) pulls the pin to its active level.
        InputSpec("door", cfg.door_pin, bool(cfg.door_pull_up),
                  lambda active: "closed" if active else "open"),
        # With a pull-up the PC817 output reads low (active) while mains is on.
        InputSpec("mains", cfg.mains_sense_pin, bool(cfg.mains_present_low),
                  lambda active: bool(active)),
    ]
    arm = tunables.siren_arm_pin()
    if arm is not None:
        specs.append(InputSpec("siren_armed", arm, True, lambda active: bool(active)))
    lid = tunables.tamper_pin()
    if lid is not None:
        specs.append(InputSpec("tamper", lid, True, lambda active: not active))
    return specs


def pin_conflicts(cfg: Any) -> list[str]:
    """Pins assigned to more than one function (a wiring/config error)."""
    used: dict[int, list[str]] = {}
    outputs = {"siren": cfg.siren_pin, "led_green": cfg.led_green_pin,
               "led_amber": cfg.led_amber_pin, "led_red": cfg.led_red_pin,
               "buzzer": cfg.buzzer_pin}
    for name, pin in outputs.items():
        used.setdefault(int(pin), []).append(name)
    for spec in input_specs(cfg):
        used.setdefault(int(spec.pin), []).append(spec.name)
    return [f"GPIO{pin}: {', '.join(names)}" for pin, names in sorted(used.items())
            if len(names) > 1]


# ── what the LEDs and the buzzer say ──────────────────────────────────────────
def _stronger(a: str, b: str) -> str:
    return a if PATTERN_ORDER.index(a) >= PATTERN_ORDER.index(b) else b


def led_patterns(live: dict[str, Any], *, setup_mode: bool = False) -> dict[str, str]:
    """The three LEDs for one snapshot of the live state (``state.get()``).

    Green — the unit works: solid when the camera is online and the Portal
    link is either off (standalone) or connected and claimed; slow blink while
    connected but not yet claimed (waiting for the owner); off otherwise.

    Amber — needs attention: fast blink on a fault (camera offline, CPU at
    the critical temperature, under-voltage now, on battery because mains is
    lost, enclosure tamper); slow blink in Wi-Fi setup mode; solid while the
    link is enabled but not connected (no network or Portal unreachable).

    Red — alarm: slow blink at a committed L3 or while an SOS request is
    open; fast blink at L4; solid at L5 or when an approved SOS could not be
    delivered (FAILED).
    """
    link = live.get("link") or {}
    node = live.get("node") or {}
    sos = live.get("sos") or {}
    level = int(live.get("level") or 1)
    camera_ok = live.get("camera_online") is not False

    linked = bool(link.get("enabled"))
    connected = bool(link.get("connected"))
    claimed = bool(link.get("claimed"))
    if not camera_ok:
        green = OFF
    elif linked and connected and not claimed:
        green = SLOW
    elif not linked or (connected and claimed):
        green = SOLID
    else:
        green = OFF

    fault = (not camera_ok or node.get("temp_state") == "critical"
             or node.get("under_voltage") is True or node.get("mains") is False
             or node.get("tamper") is True)
    if fault:
        amber = FAST
    elif setup_mode:
        amber = SLOW
    elif linked and not connected:
        amber = SOLID
    else:
        amber = OFF

    red = {3: SLOW, 4: FAST, 5: SOLID}.get(level, OFF)
    sos_state = str(sos.get("state") or "").upper()
    if sos.get("active") and sos_state in _OPEN_SOS_STATES:
        red = _stronger(red, SOLID if sos_state == "FAILED" else SLOW)
    return {"green": green, "amber": amber, "red": red}


def buzzer_events(previous: dict[str, Any] | None, live: dict[str, Any]) -> list[tuple]:
    """Chirps owed for the change from ``previous`` to ``live`` (each
    ``(on, off, n)``). Nothing on the first snapshot, so a restart is quiet.

    Committed level rises to L2/L3/L4: 1/2/3 short chirps (L5 is the siren's
    job). The Portal claims the device: one short chirp. An approved SOS turns
    FAILED (help not delivered): one long tone.
    """
    if previous is None:
        return []
    events: list[tuple] = []
    before, now = int(previous.get("level") or 1), int(live.get("level") or 1)
    if now > before and now in LEVEL_CHIRPS:
        events.append((*CHIRP_SHORT, LEVEL_CHIRPS[now]))
    if (live.get("link") or {}).get("claimed") and not (previous.get("link") or {}).get("claimed"):
        events.append((*CHIRP_SHORT, 1))
    failed_now = str((live.get("sos") or {}).get("state") or "").upper() == "FAILED"
    failed_before = str((previous.get("sos") or {}).get("state") or "").upper() == "FAILED"
    if failed_now and not failed_before:
        events.append((1.0, 0.0, 1))
    return events


# ── the service ───────────────────────────────────────────────────────────────
def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def live_pipeline() -> Any | None:
    """The process's pipeline if one is running — never builds one (a
    service thread calling ``get_pipeline`` during shutdown would load a
    second set of models)."""
    engine = sys.modules.get("sentrix.pipeline.engine")
    return getattr(engine, "_pipeline", None) if engine else None


def live_siren() -> Any | None:
    return getattr(live_pipeline(), "siren", None)


def _live_event_log() -> Any | None:
    return getattr(live_pipeline(), "events", None)


def default_audit(event: str, **detail: Any) -> None:
    events = _live_event_log()
    if events is not None:
        events.audit(event, **detail)


def default_publish(kind: str, payload: dict[str, Any]) -> None:
    from sentrix.link import publish
    publish(kind, payload)


class GpioService:
    """LEDs, buzzer and inputs (docs/7 §3.6 service; name ``gpio``).

    ``pin_factory`` overrides the shared one (tests pass a ``MockFactory``);
    ``audit``/``publish``/``timer_factory``/``siren`` replace the event log,
    the link outbox, the debounce timers and the pipeline's siren in tests;
    ``run_thread=False`` leaves the loop to the caller: ``poll_once()`` is one
    iteration.
    """

    name = "gpio"

    def __init__(self, ctx: Any, *, pin_factory: Any = None, poll_seconds: float = 0.2,
                 audit: Callable[..., None] | None = None,
                 publish: Callable[[str, dict], None] | None = None,
                 timer_factory: Callable[..., Any] | None = None,
                 siren: Callable[[], Any] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 run_thread: bool = True) -> None:
        self.ctx = ctx
        self.cfg = ctx.settings.gpio
        if pin_factory is not None:
            self.factory_info = PinFactoryInfo(pin_factory, _kind_of(pin_factory),
                                               _kind_of(pin_factory) != "mock")
        else:
            self.factory_info = shared_pin_factory()
        self.poll_seconds = poll_seconds
        self._audit = audit or default_audit
        self._publish = publish or default_publish
        self._timer_factory = timer_factory
        self._siren = siren or live_siren
        self._clock = clock
        self._run_thread = run_thread
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        self.leds: dict[str, Any] = {}
        self.buzzer: Any = None
        self.inputs: dict[str, Any] = {}
        self._debouncers: dict[str, Debouncer] = {}
        self._specs: dict[str, InputSpec] = {}
        self.values: dict[str, Any] = {}
        self.patterns: dict[str, str] = {}
        self.errors: dict[str, str] = {}
        self._previous_live: dict[str, Any] | None = None
        self._last_pir_logged: float | None = None
        self._pir_suppressed = 0
        self._last_block: dict[str, Any] | None = None
        self.buzzer_enabled = tunables.buzzer_enabled()

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop.clear()
            self._open_devices()
            self._register_arming()
        self._initial_alerts()
        self._publish_block(force=True)
        if self._run_thread:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="sentrix-gpio")
            self._thread.start()
        log.info("GPIO service started (%s pins%s)", self.factory_info.kind,
                 "" if self.factory_info.real else ", no hardware")

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._lock:
            if not self._started:
                return
            self._started = False
            from sentrix.io import actuators
            actuators.clear_arming_source(self.siren_armed)
            for debouncer in self._debouncers.values():
                debouncer.cancel()
            for device in [*self.leds.values(), self.buzzer, *self.inputs.values()]:
                if device is None:
                    continue
                try:
                    if hasattr(device, "off"):
                        device.off()
                    device.close()
                except Exception as exc:
                    log.debug("GPIO close: %s", exc)
            self.leds, self.buzzer, self.inputs = {}, None, {}
            self._debouncers.clear()
            self.patterns = {}
        log.info("GPIO service stopped")

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {"pin_factory": self.factory_info.kind, "real": self.factory_info.real,
                    "factory_error": self.factory_info.error, "leds": dict(self.patterns),
                    "inputs": dict(self.values), "errors": dict(self.errors)}

    # ── devices ───────────────────────────────────────────────────────────────
    def _open_devices(self) -> None:
        from gpiozero import LED, Buzzer, DigitalInputDevice
        factory = self.factory_info.factory
        for colour in ("green", "amber", "red"):
            pin = getattr(self.cfg, f"led_{colour}_pin")
            try:
                self.leds[colour] = LED(pin, active_high=True, initial_value=False,
                                        pin_factory=factory)
                self.patterns[colour] = OFF
            except Exception as exc:
                self.errors[f"led_{colour}"] = f"GPIO{pin}: {exc}"
        try:
            self.buzzer = Buzzer(self.cfg.buzzer_pin, active_high=True, initial_value=False,
                                 pin_factory=factory)
        except Exception as exc:
            self.errors["buzzer"] = f"GPIO{self.cfg.buzzer_pin}: {exc}"
        for spec in input_specs(self.cfg):
            try:
                device = DigitalInputDevice(spec.pin, pull_up=spec.pull_up,
                                            pin_factory=factory)
            except Exception as exc:
                self.errors[spec.name] = f"GPIO{spec.pin}: {exc}"
                continue
            self.inputs[spec.name] = device
            self._specs[spec.name] = spec
            debouncer = Debouncer(lambda d=device, s=spec: s.value(d.is_active),
                                  lambda value, n=spec.name: self._input_changed(n, value),
                                  self.cfg.input_bounce_seconds,
                                  timer_factory=self._timer_factory)
            self._debouncers[spec.name] = debouncer
            self.values[spec.name] = debouncer.value
            device.when_activated = debouncer.poke
            device.when_deactivated = debouncer.poke
        for name, error in self.errors.items():
            log.error("GPIO %s unavailable: %s", name, error)

    def _register_arming(self) -> None:
        if "siren_armed" in self.inputs:
            from sentrix.io import actuators
            actuators.set_arming_source(self.siren_armed)

    def siren_armed(self) -> bool | None:
        """The arming key's position (None without an arming input)."""
        with self._lock:
            return self.values.get("siren_armed")

    # ── inputs ────────────────────────────────────────────────────────────────
    def _initial_alerts(self) -> None:
        """Booting on battery or with the lid open is worth one alert."""
        if self.values.get("mains") is False:
            self._alert("mains_lost", {"at_start": True})
        if self.values.get("tamper") is True:
            self._alert("tamper", {"at_start": True})

    def _input_changed(self, name: str, value: Any) -> None:
        with self._lock:
            if not self._started:
                return
            self.values[name] = value
        if name == "pir":
            self._log_motion(value)
        else:
            self._audit("node_input", input=name, value=value)
        if (name == "pir" and value) or name == "door":
            forward_sensor_event(name, door=value if name == "door" else None)
        if name == "mains":
            self._alert("mains_restored" if value else "mains_lost", {})
            if not value:
                self._beep(*CHIRP_LONG, 1)
        elif name == "tamper" and value:
            self._alert("tamper", {})
            self._beep(*CHIRP_LONG, 3)
        self._publish_block()

    def _log_motion(self, motion: bool) -> None:
        if not motion:
            return
        now = self._clock()
        if (self._last_pir_logged is not None
                and now - self._last_pir_logged < PIR_LOG_INTERVAL_SECONDS):
            self._pir_suppressed += 1
            return
        self._audit("node_input", input="pir", value=True,
                    suppressed_since_last=self._pir_suppressed)
        self._last_pir_logged, self._pir_suppressed = now, 0

    def _alert(self, kind: str, detail: dict[str, Any]) -> None:
        log.warning("node alert: %s %s", kind, detail or "")
        self._audit("node_alert", kind=kind, **detail)
        try:
            self._publish("node.alert", {"kind": kind, "at": _iso_now(), "detail": detail})
        except Exception as exc:
            log.error("node.alert %s not published: %s", kind, exc)

    # ── outputs ───────────────────────────────────────────────────────────────
    def apply_patterns(self, patterns: dict[str, str]) -> None:
        for colour, pattern in patterns.items():
            led = self.leds.get(colour)
            if led is None or self.patterns.get(colour) == pattern:
                continue
            try:
                if pattern == SOLID:
                    led.on()
                elif pattern == OFF:
                    led.off()
                else:
                    on, off = BLINK_TIMES[pattern]
                    led.blink(on_time=on, off_time=off, background=True)
                self.patterns[colour] = pattern
            except Exception as exc:
                self.errors[f"led_{colour}"] = str(exc)

    def _beep(self, on: float, off: float, count: int) -> None:
        if self.buzzer is None or not self.buzzer_enabled:
            return
        try:
            self.buzzer.beep(on_time=on, off_time=off, n=count, background=True)
        except Exception as exc:
            self.errors["buzzer"] = str(exc)

    def lamp_test(self, seconds: float = 0.5) -> None:
        """Every LED on briefly at start-up, so a dead LED shows at once."""
        with self._lock:
            leds = dict(self.leds)
        for led in leds.values():
            try:
                led.on()
            except Exception:
                pass
        self._stop.wait(seconds)
        with self._lock:
            for colour, led in leds.items():
                try:
                    led.off()
                except Exception:
                    pass
                self.patterns[colour] = OFF

    # ── the loop ──────────────────────────────────────────────────────────────
    def poll_once(self) -> None:
        """Re-check the inputs, drive the LEDs and the buzzer from the live
        state, and publish the gpio block when anything changed."""
        with self._lock:
            if not self._started:
                return
            debouncers = list(self._debouncers.values())
        for debouncer in debouncers:
            debouncer.poll()
        live = self.ctx.state.get()
        with self._lock:
            if not self._started:
                return
            self.apply_patterns(led_patterns(live, setup_mode=self.ctx.settings.web.setup_mode))
            previous, self._previous_live = self._previous_live, _summary(live)
        for event in buzzer_events(previous, live):
            self._beep(*event)
        self._release_overdue_siren()
        self._publish_block()

    def _run(self) -> None:
        self.lamp_test()
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                log.error("GPIO loop: %s", exc)
            self._stop.wait(self.poll_seconds)

    def _release_overdue_siren(self) -> None:
        """Second line behind the siren's own release timer."""
        siren = self._siren()
        check = getattr(siren, "release_if_overdue", None)
        if check is not None:
            try:
                check()
            except Exception as exc:
                log.error("siren release check failed: %s", exc)

    # ── the node block ────────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        """The ``gpio`` sub-block of ``state.node`` (docs/7 §9.2)."""
        siren = self._siren()
        status = getattr(siren, "status", None)
        siren_block = status() if callable(status) else (
            None if siren is None else {"backend": siren.name, "real": siren.is_real})
        with self._lock:
            return {
                "pin_factory": self.factory_info.kind, "real": self.factory_info.real,
                "siren": siren_block,
                "leds": {c: self.patterns.get(c) for c in ("green", "amber", "red")},
                "buzzer": None if self.buzzer is None else bool(self.buzzer.is_active),
                "pir": self.values.get("pir"), "door": self.values.get("door"),
                "mains": self.values.get("mains"),
                "siren_armed": self.values.get("siren_armed"),
                "tamper": self.values.get("tamper"),
                "errors": dict(self.errors) or None,
            }

    def _publish_block(self, force: bool = False) -> None:
        from sentrix.io import actuators
        from sentrix.io.node import merge_node_block
        block = self.snapshot()
        with self._lock:
            if not force and block == self._last_block:
                return
            self._last_block = block
        merge_node_block(self.ctx.state, gpio=block, pir=block["pir"], door=block["door"],
                         mains=block["mains"], tamper=block["tamper"],
                         siren_armed=actuators.siren_armed())


def _kind_of(factory: Any) -> str:
    name = type(factory).__name__.lower()
    if "mock" in name:
        return "mock"
    return "lgpio" if "lgpio" in name else name.replace("factory", "") or "unknown"


def _summary(live: dict[str, Any]) -> dict[str, Any]:
    """The fields :func:`buzzer_events` compares, copied."""
    return {"level": live.get("level"), "link": dict(live.get("link") or {}),
            "sos": dict(live.get("sos") or {})}


# ── service factory and doctor rows ───────────────────────────────────────────
def build_service(ctx: Any) -> GpioService | None:
    """The ``gpio`` service, or None unless ``SENTRIX_GPIO_ENABLED=1``."""
    if not ctx.settings.gpio.enabled:
        return None
    return GpioService(ctx)


def preflight(settings: Any) -> list[dict[str, str]]:
    """Cheap local checks for ``tools/doctor.py`` (no pins are claimed)."""
    import importlib.util

    cfg = settings.gpio
    rows: list[dict[str, str]] = []
    model = sensors.raspberry_pi_model()
    requested = cfg.pin_factory.strip().lower() or "auto"
    if model is None:
        status = "warn" if requested == "lgpio" else "ok"
        rows.append({"status": status, "name": "pin factory",
                     "detail": f"{requested}: mock pins (not a Raspberry Pi)"})
    else:
        has_lgpio = importlib.util.find_spec("lgpio") is not None
        if requested == "mock":
            rows.append({"status": "warn", "name": "pin factory",
                         "detail": f"mock on {model}: the siren and LEDs are NOT driven"})
        else:
            rows.append({"status": "ok" if has_lgpio else "fail", "name": "pin factory",
                         "detail": f"lgpio on {model}" if has_lgpio else
                         "lgpio missing: sudo apt install python3-lgpio and a venv made "
                         "with --system-site-packages"})
    conflicts = pin_conflicts(cfg)
    rows.append({"status": "fail" if conflicts else "ok", "name": "pin map",
                 "detail": "; ".join(conflicts) if conflicts else
                 f"siren {cfg.siren_pin}, LEDs {cfg.led_green_pin}/{cfg.led_amber_pin}/"
                 f"{cfg.led_red_pin}, buzzer {cfg.buzzer_pin}, PIR {cfg.pir_pin}, door "
                 f"{cfg.door_pin}, mains {cfg.mains_sense_pin}"})
    arm = tunables.siren_arm_pin()
    armed = tunables.siren_armed_flag()
    rows.append({"status": "ok" if (arm is not None or armed) else "warn",
                 "name": "siren arming",
                 "detail": f"key switch on GPIO{arm}" if arm is not None else
                 ("no arming input; SENTRIX_SIREN_ARMED=1" if armed else
                  "SENTRIX_SIREN_ARMED=0: siren tests are refused")})
    if not settings.gpio.enabled:
        rows.append({"status": "ok", "name": "service", "detail":
                     "off (SENTRIX_GPIO_ENABLED=0): no LEDs, buzzer or inputs"})
    return rows


__all__ = ["GpioService", "build_service", "preflight", "PinFactoryInfo",
           "resolve_pin_factory", "shared_pin_factory", "Debouncer", "InputSpec",
           "input_specs", "pin_conflicts", "led_patterns", "buzzer_events",
           "OFF", "SLOW", "FAST", "SOLID", "MOCK_REVISION"]
