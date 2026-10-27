# SENTRIX on a Raspberry Pi 5 — bring-up and deployment

The Pi runs the same device process as the laptop (`python -m sentrix`:
pipeline, Local Console, SOS, channels, Portal link) with the Camera Module 3,
the siren relay, status LEDs, PIR, door contact and mains sensing (no buzzer
and no speaker: D19).
Design, pinout, power and parts: [`docs/8_RASPBERRY_PI5_INTEGRATION.md`](../../docs/8_RASPBERRY_PI5_INTEGRATION.md)
(§6 for wiring). Pin numbers in code: `settings.gpio` in `sentrix/config.py`.

**Status:** these files are tested on the laptop against fakes
(`tests/test_hw_*.py`). None of this has run on a real Pi yet; every step below
that needs the hardware is still to be done.

**Safety:** nothing here sends an alert. Test mode is the default
(`SENTRIX_TEST_MODE=1`); a channel sends only with `SENTRIX_LIVE_ALERTS=1` plus
its own switch, and then only to `SENTRIX_TEST_RECIPIENTS`. Keep it that way.

## Files

| File | Goes to | What it does |
|---|---|---|
| `install_pi.sh` | run once, as root | apt packages, service user, venv **with** `--system-site-packages`, `requirements-pi.txt`, `.env`, systemd, journald, watchdog, NetworkManager, polkit; optional config.txt, firewall, 4G profile |
| `pi.env` | merged into `.env` by `merge_env.py` | Pi defaults: `CAMERA_SOURCES=picamera:0` at 15 fps, clip mode on ONNX with the fast lane, GPIO and node services on, `buffalo_s`, LAN binding, test mode, SOS and channels on, modem and link off |
| `merge_env.py` | used by the installer | adds missing keys from `pi.env`; never changes a key `.env` already sets (on a fresh `.env` it replaces the template's values) |
| `systemd/sentrix.service.in` | `/etc/systemd/system/sentrix.service` | `Type=notify`, `Restart=always`, `WatchdogSec=30`; siren pin forced off before start and after any exit |
| `systemd/10-sentrix-journald.conf` | `/etc/systemd/journald.conf.d/` | journal ≤ 200 MB, 1 GB kept free, one month |
| `systemd/10-sentrix-watchdog.conf` | `/etc/systemd/system.conf.d/` | systemd pets the hardware watchdog (10 s) |
| `config.txt.snippet` | appended to `/boot/firmware/config.txt` | `gpio=17=op,dl`, RTC charging, `kernel_watchdog_timeout=10`, camera, I2C, optional sensor overlays, the power note |
| `networkmanager/20-sentrix-connectivity.conf` | `/etc/NetworkManager/conf.d/` | connectivity check: a link without Internet gets +20000 on its metric, so 4G takes over |
| `networkmanager/sentrix-modem-gsm.nmconnection.in` | `system-connections/` (0600), `--modem-apn` | 4G through ModemManager, route metric 700 |
| `networkmanager/sentrix-modem-ecm.nmconnection.in` | `system-connections/` (0600), `--modem-ecm` | 4G dongle in ECM/RNDIS mode (`usb0`), metric 700 |
| `networkmanager/sentrix-wifi.nmconnection.in` | by hand (or `/setup/wifi`) | home Wi-Fi, metric 600 |
| `networkmanager/sentrix-setup-hotspot.nmconnection.in` | by hand | the setup hotspot for `/setup/wifi` (never autoconnects) |
| `polkit/50-sentrix-networkmanager.rules` | `/etc/polkit-1/rules.d/` | lets the service user run `/setup/wifi`'s nmcli calls |
| `ufw.sh` | run once, as root | inbound: SSH and the console from the LAN and the setup hotspot only |

## What you need

The bench kit of docs/8 §8 (Pi 5 8 GB, 27 W supply, Active Cooler, High
Endurance card, Camera Module 3 Wide with the 22-pin Standard–Mini cable,
ML-2020 cell, USB microphone, relay parts, PIR, reed switch, LEDs), an
LED + 1 kΩ resistor for the relay test, ear protection, and the laptop with
the repository (for model exports and provisioning).

## Bring-up order

Do the steps in this order; each one proves something the next relies on.

### 1. Card and OS

1. Flash Raspberry Pi OS **Trixie** (or Bookworm) **64-bit Lite** to the High
   Endurance card. Set a host name, your SSH public key and the Wi-Fi in the
   imager. SSH keys only.
2. Fit the Active Cooler (mandatory: uncooled, the Pi 5 throttles at
   80–85 °C) and the ML-2020 RTC cell (never a CR2032).
3. Power from the official 27 W supply for now. The 12 V UPS path comes in
   step 13.

### 2. Camera

Camera Module 3 on CAM/DISP 0 with the Standard–Mini cable, before the
cooler blocks the connector. Boot and check:

```bash
rpicam-hello --list-cameras        # must list the imx708 (Camera Module 3)
```

### 3. Install

```bash
sudo apt update && sudo apt install -y git
sudo git clone <the repository> /opt/sentrix && cd /opt/sentrix
sudo deploy/pi5/install_pi.sh --boot-config --ufw 192.168.1.0/24   # your LAN
sudo rpi-eeprom-config --edit      # add PSU_MAX_CURRENT=5000 (needed for the 12 V buck and the modem)
sudo reboot
```

`install_pi.sh --dry-run` prints every action without changing anything.
Options: `--user NAME`, `--no-apt` (re-run offline), `--no-service`,
`--start`, `--modem-apn APN`, `--modem-ecm [IF]`. The installer creates
`.env` with generated secrets (`tools/setup_env.py`), merges `pi.env` into it,
and creates `/etc/sentrix` (0700) for the device identity.

After the reboot:

```bash
vcgencmd get_throttled             # throttled=0x0
pinctrl get 17                     # output, driven low (op dl ... lo)
```

### 4. Doctor

```bash
sudo -u sentrix /opt/sentrix/.venv/bin/python /opt/sentrix/tools/doctor.py
```

The hardware section checks the venv's system site packages, the camera
(enumerated, not opened), OpenCV < 5, temperature, throttling, EXT5V, the
config.txt lines, `PSU_MAX_CURRENT`, the systemd unit and the network. The
services section prints the GPIO pin map and the siren arming state, and the
exported models the runtime will use.

### 5. Exported models

`pi.env` sets `SENTRIX_RUNTIME=onnx`. The exports are built on the laptop and
are not in git:

```bash
# on the laptop
python tools/export_models.py --scene          # writes models/exported/ and checks parity
scp -r models/exported pi@<pi>:/tmp/ && ssh pi@<pi> 'sudo cp -r /tmp/exported /opt/sentrix/models/ && sudo chown -R sentrix: /opt/sentrix/models/exported'
```

A missing export falls back to torch (slow on the Pi). `--ncnn` also writes
NCNN exports, but needs `ncnn`/`pnnx`, which were never installed, so NCNN is
untested.

### 6. Inputs (siren not connected)

```bash
sudo systemctl start sentrix && journalctl -u sentrix -f
```

Open the Node page (`http://<pi>:8000/node`, or `GET /api/node`):

| Do | Expect |
|---|---|
| Wave at the PIR | `pir: true` |
| Open the door contact | `door: "open"` |
| Pull mains (once the mains sense is wired) | `mains: false`, amber LED fast blink, a `node_alert` row in the event log |

Debounce is 50 ms in software; check it does not miss or double real edges.

### 7. LEDs

At start the three LEDs light for 0.5 s (lamp test). Then green solid =
running; amber solid = link enabled but not connected; amber fast = a fault;
red slow blink at L3 or with an open SOS request, fast at L4, solid at L5.
Full table: docs/8 §6.5.

### 8. The relay test (LED in place of the siren)

The siren is wired last. First prove the relay **never** closes when it should
not, with an LED + 1 kΩ resistor on the relay's NO contact (from 5 V or a
3 V coin cell) where the siren will go:

| Event | LED must stay | How |
|---|---|---|
| Cold start (power off 10 s, on) | off | `gpio=17=op,dl` holds the pin low from the firmware on |
| Reboot | off | `sudo reboot` |
| Service stop | off | `sudo systemctl stop sentrix` |
| Crash | off | `sudo kill -9 $(systemctl show -p MainPID --value sentrix)` (`ExecStopPost` forces the pin low; systemd restarts it) |
| Mains → UPS switchover | off | pull the mains plug of the UPS (after step 13) |
| Test | **on for 1 s**, then off | Node page "Test siren" (once per minute) |

If the LED lights at boot or stays lit after a stop, the polarity is wrong:
an active-low opto relay board needs `SENTRIX_SIREN_ACTIVE_HIGH=0` in `.env`
and `gpio=17=op,dh` in config.txt. Set the first, then re-run
`sudo deploy/pi5/install_pi.sh --no-apt`, which renders the unit's
`pinctrl ... dh`; edit the config.txt line by hand (the installer does not
rewrite an existing SENTRIX block) and reboot. With the NPN driver
(1 kΩ base, 4.7 kΩ base pull-down, 1N4007 across the coil) the relay is
active-high and the defaults are right.

### 9. Siren

Connect the siren to the relay's NO contact on the fused 12 V line through
the arming key (docs/8 §6.3). Ear protection on (110 dB). One test from the
Node page (`POST /api/node/siren-test`: 1 s, once per minute; device admin, or
an owner with a fresh step-up through the Portal).

**Arming key.** Wire the key's second contact from a free GPIO to GND and set
`SENTRIX_SIREN_ARM_PIN=<gpio>`: the Node page shows the key's position and a
siren test is refused (409) while it is off. Without that input,
`SENTRIX_SIREN_ARMED=1` (default) says the line is armed. A real alarm drives
the relay either way; the key is what silences the line.

### 10. Wi-Fi and 4G failover (data)

Wi-Fi has metric 600, the modem 700, set explicitly (an ECM dongle would
otherwise be an Ethernet device at 100 and carry everything). Use an Airtel or
Vi SIM, not Jio.

```bash
# modem managed by ModemManager (needs: sudo apt install modemmanager)
sudo deploy/pi5/install_pi.sh --no-apt --no-service --modem-apn airtelgprs.com   # Vi: www
# or a dongle that appears as an Ethernet device (usb0)
sudo deploy/pi5/install_pi.sh --no-apt --no-service --modem-ecm
```

Test: turn off the upstream Wi-Fi's Internet (e.g. the hotspot's mobile data).
Within the connectivity interval (30 s), `ip route show default` shows the
modem carrying the default route; turn it back on and Wi-Fi takes over again.

### 11. Modem SMS (Tier 1, test mode)

SMS goes through AT commands on the modem's serial port, separate from the
data connection. In `.env`:

```
SENTRIX_MODEM_ENABLED=1
SENTRIX_MODEM_PORT=          # empty = probe; or /dev/ttyUSB2; or mmcli if ModemManager owns the port
```

Restart and check `GET /api/channels` (`modem: present, registered,
signal_dbm`). A real test SMS to a team phone needs, all at once:
`SENTRIX_LIVE_ALERTS=1`, `SENTRIX_MODEM_LIVE=1`, the phone in
`SENTRIX_TEST_RECIPIENTS`, and the number in the current profile (owner,
confirmed contact or Desk); at most 30 segments a day. Set the two live
switches back to 0 afterwards. Check PDU mode, delivery reports and
registration on this SIM.

### 12. Link to the Portal

The Portal must be running first ([`deploy/portal/README.md`](../portal/README.md)).

1. Provision the device on the Portal (company side). It writes
   `identity.json` and a printable label with the claim code and setup code.
2. Copy `identity.json` to the Pi:
   ```bash
   sudo install -m 600 -o sentrix -g sentrix identity.json /etc/sentrix/identity.json
   ```
3. In `.env`: `SENTRIX_LINK_ENABLED=1` and
   `SENTRIX_PORTAL_URL=https://portal.<yourdomain>.in`. Restart.
4. Green LED slow blink = connected, not yet claimed. The customer claims the
   device in onboarding step 5 with the label's code or QR; the green LED
   turns solid.

**Setup Wi-Fi without a screen (optional).** Fill the `@...@` fields of
`sentrix-setup-hotspot.nmconnection.in` and install it to
`/etc/NetworkManager/system-connections/` (0600). With `SENTRIX_SETUP_MODE=1`,
`nmcli connection up id sentrix-setup` brings up the hotspot; the phone opens
`http://10.42.0.1:8000/setup/wifi` and enters the label's setup code.

### 13. Power (12 V UPS path)

Wire docs/8 §6.3: BIS-marked 12 V adapter → router mini UPS → fused 12 V bus →
F1 → XY-3606 buck (5.2 V) → short USB-C → Pi; F2 → arming key → relay → siren;
PC817 mains sense on the adapter output. Keep the UPS and its cells outside
the box. Pass test:

```bash
vcgencmd pmic_read_adc | grep EXT5V   # >= 4.8 V with the siren pulsing and the modem sending
vcgencmd get_throttled                # 0x0
```

Then 10 mains pulls with no reboot, and the relay test's UPS row again.
Measure the UPS runtime (the plan's ≈ 2 h+ is an estimate).

### 14. Gate A measurement

Stop the service first (the bench needs the CPU to itself and refuses to run
with live services):

```bash
sudo systemctl stop sentrix
cd /opt/sentrix          # the clone belongs to root, so these run with sudo
sudo .venv/bin/python tools/bench_clip.py --runtime onnx --fast-lane --seconds 3600 --json /tmp/bench_clip_pi.json
sudo .venv/bin/python tools/replay_custom_videos.py --videos all --mode clip --fast-lane --threads 4 --tag pi \
    --compare training/runs/custom_eval/clip/replay_all_fastlane.json
```

The bench uses a custom video as the camera and stubs every side effect. The
replay is evaluation only: report its numbers, never tune on them (and never
on `wa02`).

Then run the service with the link on for an hour and read the clip block
(compute p50/p95, dropped clips) on the Node page, plus temperature and
throttling. Criteria: docs/8 §7.

## Operating notes

* Logs: `journalctl -u sentrix -f`. Runtime data: `/opt/sentrix/var/`
  (evidence, events, database). Identity: `/etc/sentrix/`.
* The console listens on the LAN (`SENTRIX_HOST=0.0.0.0`; ufw admits only the
  LAN and the setup hotspot) over plain HTTP: accepted for the demo, stated in
  the report. Remote access goes through the Portal's tunnel, never an open
  port.
* Read-only root (optional, after bring-up): `sudo raspi-config nonint
  do_overlayfs 0`. This also makes `/boot` read-only, so later config.txt or
  EEPROM edits need `do_overlayfs 1` and a reboot first.
* Clone the finished card as a spare. Keep `EVIDENCE_AES_KEY` and `.env` off the
  device as well (the Pi has no TPM; losing the key makes every bundle
  unreadable).
* Laptop development needs none of this: `./run.sh` detects a Pi and only then
  uses `--system-site-packages` and `requirements-pi.txt`.
