# SENTRIX on a Raspberry Pi 5 — bring-up and deployment

The same device process as on the laptop (`python -m sentrix`: pipeline, Local
Console, SOS, channels, link), on a Pi 5 with the Camera Module 3, the siren
relay, status LEDs, buzzer, PIR, door contact and mains sensing. Plan and
reasoning: `docs/8_RASPBERRY_PI5_INTEGRATION.md`; pins and polarity: §6 there and `sentrix/io/gpio.py`.

| File | Goes to | What it does |
|---|---|---|
| `install_pi.sh` | run once, as root | apt packages, service user, venv **with** `--system-site-packages`, `requirements-pi.txt`, `.env`, systemd, journald, watchdog, NetworkManager, polkit; optional config.txt, firewall, 4G profile |
| `systemd/sentrix.service.in` | `/etc/systemd/system/sentrix.service` | `Type=notify`, `Restart=always`, `WatchdogSec=30`, siren pin forced off before start and after any exit |
| `systemd/10-sentrix-journald.conf` | `/etc/systemd/journald.conf.d/` | journal ≤ 200 MB, 1 GB kept free, one month |
| `systemd/10-sentrix-watchdog.conf` | `/etc/systemd/system.conf.d/` | systemd pets the hardware watchdog (10 s) |
| `config.txt.snippet` | appended to `/boot/firmware/config.txt` | `gpio=17=op,dl`, `rtc_bbat_vchg`, `kernel_watchdog_timeout=10`, camera, I2C, sensor overlays, PSU note |
| `pi.env` | merged into `.env` | Pi defaults: `CAMERA_SOURCES=picamera:0`, GPIO and node services on, LAN binding, `buffalo_s` |
| `networkmanager/*.nmconnection.in` | `/etc/NetworkManager/system-connections/` (0600) | Wi-Fi metric 600, 4G modem metric 700 (ModemManager or ECM dongle), setup hotspot |
| `networkmanager/20-sentrix-connectivity.conf` | `/etc/NetworkManager/conf.d/` | connectivity check: a link without Internet gets +20000, so 4G takes over |
| `polkit/50-sentrix-networkmanager.rules` | `/etc/polkit-1/rules.d/` | lets the service user run `/setup/wifi`'s nmcli calls |
| `ufw.sh` | run once, as root | inbound: SSH and the console from the LAN (and the setup hotspot) only |

## Bring-up order

Do the steps in this order; each one proves something the next relies on.
Nothing here sends an alert: test mode is the default (`SENTRIX_TEST_MODE=1`)
and every channel stays a dry run until its two switches are on.

1. **OS.** Raspberry Pi OS Trixie (or Bookworm) 64-bit Lite on a high-endurance
   microSD. Fit the Active Cooler (mandatory: uncooled, the Pi 5 throttles at
   80–85 °C under load) and the ML-2020 RTC cell (never a CR2032). SSH keys
   only.
2. **Power.** Official 27 W supply, or the 12 V UPS → 5.2 V buck path. For a
   non-PD supply set `PSU_MAX_CURRENT=5000` with `sudo rpi-eeprom-config
   --edit` (otherwise USB gets 600 mA and the 4G modem browns out).
3. **Camera.** Camera Module 3 on CAM/DISP 0 with the 22-pin Standard–Mini
   cable. `rpicam-hello --list-cameras` must list it.
4. **Software.**
   ```bash
   sudo git clone <repo> /opt/sentrix && cd /opt/sentrix
   sudo deploy/pi5/install_pi.sh --boot-config --ufw 192.168.1.0/24
   sudo reboot
   ```
   After the reboot: `vcgencmd get_throttled` → `throttled=0x0`;
   `pinctrl get 17` shows the pin as an output driven low (`op dl … lo`).
5. **Doctor.** `sudo -u sentrix /opt/sentrix/.venv/bin/python
   /opt/sentrix/tools/doctor.py` — the hardware section checks the venv,
   the camera (enumerated, not opened), OpenCV < 5, temperature,
   throttling, EXT5V, the config.txt lines, `PSU_MAX_CURRENT`, the unit and
   the network; the services section prints the GPIO pin map and arming.
6. **Inputs** (siren not connected). Start the service and watch the Node
   page (`GET /api/node`): wave at the PIR (`pir: true`), open the door
   (`door: "open"`), pull mains on the UPS (`mains: false`, amber LED fast
   blink, one long beep, a `node_alert` row in the event log).
7. **LEDs and buzzer.** At start the three LEDs light for 0.5 s (lamp test).
   Green solid = running; amber solid = no network; red blinks at L3.
8. **Relay test with an LED in place of the siren** — see below. Only then
   connect the siren.
9. **Siren.** Connect the siren to the relay's NO contact on its own fused
   12 V supply, ear protection on (110 dB; safe daily dose ≈ 90 s at the rated
   distance). One test from the Node page (`POST /api/node/siren-test`: 1 s,
   once per minute, owner with a fresh step-up or the device admin).
10. **Network failover.** Wi-Fi (metric 600) and the 4G modem (700, set
    explicitly: an ECM dongle would otherwise be an Ethernet device at 100 and
    carry everything): `install_pi.sh --no-apt --no-service --modem-apn
    airtelgprs.com` (or `--modem-ecm`). Turn the hotspot's mobile data off:
    within the connectivity interval (30 s) `nmcli -t -f DEVICE,STATE device`
    shows the modem carrying the default route (`ip route show default`).

## The relay test (LED in place of the siren)

The siren is wired last. First prove the relay **never** closes when it should
not, with an LED + 1 kΩ resistor on the relay's NO contact (from 5 V or a
3 V coin cell) where the siren will go:

| Event | LED must stay | How |
|---|---|---|
| Cold start (power off 10 s, on) | off | `gpio=17=op,dl` holds the pin low from the firmware on |
| Reboot | off | `sudo reboot` |
| Service stop | off | `sudo systemctl stop sentrix` |
| Crash | off | `sudo kill -9 $(systemctl show -p MainPID --value sentrix)` (ExecStopPost forces the pin low; systemd restarts it) |
| Mains → UPS switchover | off | pull the mains plug of the UPS |
| Test | **on for 1 s**, then off | Node page "Test siren" (once per minute) |

If the LED lights at boot or stays lit after a stop, the polarity is wrong:
an active-low opto relay board needs `SENTRIX_SIREN_ACTIVE_HIGH=0` in `.env`
and `gpio=17=op,dh` in config.txt (re-run `install_pi.sh --no-apt`, which
renders the unit's `pinctrl ... dh` too). With the NPN driver of the roadmap
(1 kΩ base, 4.7 kΩ base pull-down, 1N4007 across the coil) the relay is
active-high and the defaults are right.

## Siren arming switch

The key switch on the siren's 12 V line is the physical disarm. Wire its
second contact from a free GPIO to GND and set `SENTRIX_SIREN_ARM_PIN=<gpio>`:
the Node page then shows the key's position and a siren test is refused (409)
while it is off. Without that input, `SENTRIX_SIREN_ARMED=1` (default) says
the line is armed. A real alarm drives the relay either way.

## Operating notes

* Logs: `journalctl -u sentrix -f`. Runtime data: `/opt/sentrix/var/`
  (evidence, events, database), identity: `/etc/sentrix/`.
* The console listens on the LAN (`SENTRIX_HOST=0.0.0.0`, ufw admits only the
  LAN) over plain HTTP: accepted for the demo, stated in the report. Remote
  access goes through the Portal's tunnel, never an open port.
* Read-only root (optional, after bring-up): `sudo raspi-config nonint
  do_overlayfs 0` — this also makes `/boot` read-only, so later config.txt or
  EEPROM edits need `do_overlayfs 1` and a reboot first. Clone the finished
  card as a spare, and keep `EVIDENCE_AES_KEY` and `.env` off the device
  (the Pi has no TPM; losing the key makes every bundle unreadable).
* Laptop development needs none of this: `./run.sh` detects a Pi and only
  then uses `--system-site-packages` and `requirements-pi.txt`.
