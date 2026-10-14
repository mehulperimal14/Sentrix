# T-HW integration notes (Raspberry Pi 5 hardware I/O and deployment)

Branch `wip/hw`, based on `sentrix/portal` (foundation `31c6c50`). Every file
below is T-HW-owned per docs/7 §2.3/§2.5; nothing in §2.2 was edited. The one
document edit outside `sentrix/io/**` is HARDWARE.md §6 (pin table and relay
polarity), which docs/7 §3.4 and §16 q3 assign to T-HW.

## What landed

| File | What |
|---|---|
| `sentrix/io/actuators.py` | `GpioSiren` rewritten on gpiozero (`OutputDevice`, lgpio on the Pi 5; RPi.GPIO does not work there). Active-high by default (NPN driver), `active_low` for opto boards, claimed OFF, release timer, 90 s maximum on-time per period (extensions capped), `release_if_overdue()` second line, off at exit, `status()`. Siren arming (`set_arming_source`, `siren_arming`, `siren_armed`; `Actuator.armed`). `siren_test` refuses a disarmed line with `conflict` before the limiter; `siren_test_retry_after()`. `build_siren` keeps its API and the console/sound behaviour; **`SENTRIX_SIREN=auto` (what `.env.example` ships) now means auto** — before, it fell through to the console stub on every install made from the template |
| `sentrix/io/gpio.py` | `build_service`/`preflight`; process-wide pin factory (`""` auto = lgpio on a Pi else `MockFactory` as a Pi 5; lgpio failure on a Pi = mock pins **plus a visible error**); LEDs GPIO5/12/13 with pairing/fault/level patterns (`led_patterns`), buzzer GPIO26 chirps (`buzzer_events`), inputs PIR 23, door 24 (pull-up), mains 16 (PC817, low = present), optional arming key and enclosure tamper; software debounce + poll fallback; `node_input`/`node_alert` audit rows; `node.alert` (`mains_lost`, `mains_restored`, `tamper`) via `sentrix.link.publish` |
| `sentrix/io/node.py` | `build_service`/`preflight`; telemetry every 5 s into `state.node` (`merge_node_block`, the single writer for T-HW's block); `over_temp` and `under_voltage` node alerts with hysteresis; systemd `READY=1` + watchdog pings only while the event loop answers and the live loop thread runs; notify-only service under systemd even with `SENTRIX_NODE_ENABLED=0` (a `Type=notify` + `WatchdogSec` unit dies without it); `hardware_report()` for the doctor |
| `sentrix/io/sensors.py` | CPU temperature (sysfs, then `vcgencmd measure_temp`), `get_throttled` decoded, `pmic_read_adc` EXT5V + board power estimate, uptime, BME280/DHT22 through the kernel IIO drivers behind flags; all None off a Pi |
| `sentrix/io/watchdog.py` | `SystemdNotifier` (sd_notify datagrams, no libsystemd) |
| `sentrix/io/network.py` | nmcli: `scan()`, `connect(ssid, psk)` (metric-600 profile, PSK never logged), `status()` (`via` wifi/ethernet/modem; an ECM dongle reads as modem) |
| `sentrix/io/picamera.py` | `PiCameraSource(FrameSource)`: one Picamera2 request → main 1280×720 (`latest()`/`read()`) + lores 640×480 (`latest_lores()`, same seq), BGR, `SensorTimestamp` capture time, Pi 4 YUV420 lores fallback, every request released, reconnect, guarded import with the apt/venv hint; spec `picamera`, `picamera:N`, `?fps=&main=WxH&lores=WxH` |
| `sentrix/io/sources.py` | `build_source` accepts the `picamera` specs (names stay redacted) |
| `sentrix/io/tunables.py` | adapter for the fields below until they are in `config.py` |
| `sentrix/web/node_routes.py` | `GET /api/node` (state.node + siren, arming, test limiter, camera, services); `POST /api/node/siren-test` (409 disarmed, 429 + `Retry-After`) |
| `tools/doctor.py` | `check_hardware` (Pi-only rows, one "skipped" row on a laptop); the webcam probe is skipped when `CAMERA_SOURCES` is a Picamera2 source (it cannot open the CSI camera, and the service may hold it) |
| `run.sh` | Pi detection: `--system-site-packages` + `requirements-pi.txt` + the OpenCV swap; laptop path unchanged; `SENTRIX_RUN_DRY_RUN=1` |
| `requirements-pi.txt` | header: apt line incl. `libportaudio2`, the ultralytics → `opencv-python` conflict and how it is resolved |
| `deploy/pi5/**` | `install_pi.sh`, `merge_env.py`, `pi.env`, `systemd/sentrix.service.in` (`Type=notify`, `Restart=always`, `WatchdogSec=30`, `pinctrl` forces the siren pin off before start and after any exit), journald caps, `RuntimeWatchdogSec` drop-in, `config.txt.snippet` (`gpio=17=op,dl`, `rtc_bbat_vchg`, `kernel_watchdog_timeout=10`, sensor overlays, `PSU_MAX_CURRENT` note), NetworkManager Wi-Fi 600 / gsm 700 / ECM 700 / setup hotspot templates and the connectivity check, polkit rule, `ufw.sh`, `README.md` (bring-up order, LED-in-place-of-siren relay test) |
| `HARDWARE.md` §6 | pin table (GPIO17 active-high NPN driver, LEDs 5/12/13 on pins 29/32/33, mains sense GPIO16, arming key), relay paragraph, bring-up safety |
| `tests/test_hw_*.py` | 193 tests: siren, gpio service, node/telemetry/watchdog/doctor rows, picamera, network, routes, deploy files (bash dry runs where a POSIX bash exists) |

## Shared-file changes requested (integration owner)

1. **`sentrix/config.py`** — add these fields (env names already read by
   `sentrix/io/tunables.py`, which prefers the field once it exists, so adding
   them changes no behaviour):

   | Field | Env | Default | Meaning |
   |---|---|---|---|
   | `gpio.siren_max_on_seconds` | `SENTRIX_SIREN_MAX_ON_SECONDS` | 90.0 | one relay on-period never exceeds this |
   | `gpio.siren_arm_pin` | `SENTRIX_SIREN_ARM_PIN` | None | GPIO of the arming key's second contact (to GND = armed) |
   | `gpio.siren_armed` | `SENTRIX_SIREN_ARMED` | True | arming state without that input; False refuses tests |
   | `gpio.tamper_pin` | `SENTRIX_TAMPER_PIN` | None | enclosure lid switch (to GND = shut) |
   | `gpio.buzzer_enabled` | `SENTRIX_BUZZER_ENABLED` | True | local chirps |
   | `node.bme280` / `node.dht22` | `SENTRIX_SENSOR_BME280` / `SENTRIX_SENSOR_DHT22` | False | IIO room sensors |
   | `node.picamera_main` / `node.picamera_lores` | `SENTRIX_PICAMERA_MAIN` / `SENTRIX_PICAMERA_LORES` | "1280x720" / "640x480" | Picamera2 stream sizes |

   `test_config_never_publishes_secrets` stays green (no secret names).
2. **`.env.example`** — document the env vars above under "hardware"; note
   that `SENTRIX_SIREN=auto` is now honoured (it used to select the console
   stub); add `SENTRIX_SIREN_ACTIVE_HIGH=0` + `gpio=17=op,dh` for opto boards;
   point Pi users at `deploy/pi5/README.md` (`CAMERA_SOURCES=picamera:0`).
3. **`sentrix/pipeline/state.py` `NODE_IDLE`** (optional) — add the keys the
   services write so the idle block has the running shape: `temp_state`,
   `throttled_raw`, `throttle_flags`, `under_voltage_occurred`, `power_w`,
   `environment`, `tamper`, `platform`, `telemetry`.
4. **`sentrix/app.py`** — expose the live loop's liveness, e.g.
   `def live_loop_alive() -> bool` (`_thread` alive or `_stop` set). Today
   `sentrix.io.node.live_loop_alive` reads `sentrix.app._thread` / `_stop`
   defensively (treats "absent" as alive).
5. **`tests/conftest.py`** — add `"NOTIFY_SOCKET": ""` (and `WATCHDOG_USEC`)
   to `_FORCED_ENV`, so a suite run under systemd never builds the notify-only
   node service in a lifespan test; after each test also call
   `sentrix.io.actuators.clear_arming_source()` and
   `sentrix.io.gpio._reset_shared_pin_factory()` (the T-HW tests do it
   themselves today).
6. **docs/7** — §13 device audit events: add `node_input` (input, value) and
   `node_alert` (kind, detail); §9.2 `/api/node`: extra keys `siren`,
   `siren_arming`, `siren_test`, `camera`, `services`; siren-test answers 409
   `conflict` while the siren line is disarmed; §2.2 frozen names:
   `siren_test`'s result gained `armed`, `Actuator` gained `armed` and
   `status()` (additive); §3.4: settled polarity = active-high (q3 closed).
7. **`CLAUDE.md`** M10: T-HW status (this file); **README**: a Raspberry Pi
   section pointing at `deploy/pi5/README.md`.

## Notes for other tracks

* **T-LINK** — `policy.py`: `POST /api/node/siren-test` = owner + step-up
  ≤ 300 s; the 1/min limit lives in `actuators.siren_test` (shared with the
  `siren.test` command), so policy does not need its own. Map the command's
  `conflict` (disarmed) like the route does. The heartbeat's `node` fields
  (`cpu_temp_c`, `throttled`, `under_voltage`, `mains`, `ext5v_v`) are in
  `state.get()["node"]`. T-HW publishes `node.alert` itself.
* **T-WEB** — Node page: everything is in `GET /api/node`; the test button
  POSTs with `X-CSRF-Token` and shows 409 ("siren disarmed") and 429
  (`Retry-After`). `/setup/wifi`: `sentrix.io.network.scan() / connect(ssid,
  psk) / status()`. `node_routes` only requires a principal (`require_lc()`),
  per `sentrix/web/auth.py`.
* **T-CLIP** — `PiCameraSource.latest_lores(after_seq)` →
  `(frame, seq, captured_at)`, same seq/time as `latest()`. Other sources have
  no lores stream: use `getattr(source, "latest_lores", None)` and fall back to
  the main frame.
* **T-SOS** — the red LED shows `state.sos` (open request: slow blink; FAILED:
  solid, plus a 1 s tone).
* **T-CH** — the unit runs in groups `dialout` (modem tty) and the 4G data
  profile (metric 700) is independent of the AT-command SMS path.

## What remains, and why

Needs the hardware on the bench (all code paths exist and are tested with
gpiozero's `MockFactory`, a fake `picamera2`, a fake `vcgencmd`, a fake nmcli
and a fake sysfs tree):

* the relay test of `deploy/pi5/README.md` on the real board (polarity, cold
  start, reboot, stop, `kill -9`, mains → UPS) and the 50 ms debounce with real
  PIR / reed / PC817 signals;
* Picamera2 on the Camera Module 3 Wide: RGB888 lores on the Pi 5, that
  `SensorTimestamp` shares `time.monotonic()`'s clock (else arrival time is
  used), CPU cost of 1280×720 at 15 fps through the live loop;
* `vcgencmd pmic_read_adc` line format on the unit's firmware, IIO overlay
  names for the BME280/DHT22, `wdctl` limits behind `kernel_watchdog_timeout`;
* NetworkManager failover with the actual dongle (ModemManager `gsm` vs ECM
  `usb0`), the connectivity check from the demo network, the polkit actions on
  Trixie, `pinctrl` on the image;
* which free GPIO carries the arming key's sense contact (optional; without
  it `SENTRIX_SIREN_ARMED` states the key).

Out of scope here: the multi-process Pi layout (deferred by the contract), the
IMX500 tensor reader (T4), the clip engine's use of the lores stream (T-CLIP).
