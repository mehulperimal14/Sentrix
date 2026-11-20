# SENTRIX on a Raspberry Pi 5 — bring-up and deployment

The Pi runs the same device process as the laptop (`python -m sentrix`:
pipeline, Local Console, SOS, channels, Portal link) with the Camera Module 3,
status LEDs, PIR, door contact and mains sensing (no siren since D25, no
buzzer and no speaker since D19).
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
| `systemd/sentrix.service.in` | `/etc/systemd/system/sentrix.service` | `Type=notify`, `Restart=always`, `WatchdogSec=30` |
| `systemd/10-sentrix-journald.conf` | `/etc/systemd/journald.conf.d/` | journal ≤ 200 MB, 1 GB kept free, one month |
| `systemd/10-sentrix-watchdog.conf` | `/etc/systemd/system.conf.d/` | systemd pets the hardware watchdog (10 s) |
| `systemd/sentrix-user.service` | `~/.config/systemd/user/sentrix.service` | the demo unit's way instead: a **user** service from `~/Sentrix` (`Restart=on-failure`, no watchdog); see "The demo unit" below |
| `config.txt.snippet` | appended to `/boot/firmware/config.txt` | RTC charging, `kernel_watchdog_timeout=10`, camera, I2C, optional sensor overlays, the power note |
| `networkmanager/20-sentrix-connectivity.conf` | `/etc/NetworkManager/conf.d/` | connectivity check: a link without Internet gets +20000 on its metric, so 4G takes over |
| `networkmanager/sentrix-modem-gsm.nmconnection.in` | `system-connections/` (0600), `--modem-apn` | 4G through ModemManager, route metric 700 |
| `networkmanager/sentrix-modem-ecm.nmconnection.in` | `system-connections/` (0600), `--modem-ecm` | 4G dongle in ECM/RNDIS mode (`usb0`), metric 700 |
| `networkmanager/sentrix-wifi.nmconnection.in` | by hand (or `/setup/wifi`) | home Wi-Fi, metric 600 |
| `networkmanager/sentrix-setup-hotspot.nmconnection.in` | by hand | the setup hotspot for `/setup/wifi` (never autoconnects) |
| `polkit/50-sentrix-networkmanager.rules` | `/etc/polkit-1/rules.d/` | lets the service user run `/setup/wifi`'s nmcli calls |
| `ufw.sh` | run once, as root | inbound: SSH and the console from the LAN and the setup hotspot only |

## What you need

The bench kit of docs/8 §8 (Pi 5 8 GB, 27 W supply, Active Cooler, High
Endurance card, Camera Module 3 (standard lens; hardware/enclosure fits only it) with the 22-pin Standard–Mini cable,
ML-2020 cell, USB microphone, PIR, reed switch, LEDs) and the laptop with
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
```

### 4. Doctor

```bash
sudo -u sentrix /opt/sentrix/.venv/bin/python /opt/sentrix/tools/doctor.py
```

The hardware section checks the venv's system site packages, the camera
(enumerated, not opened), OpenCV < 5, temperature, throttling, EXT5V, the
config.txt lines, `PSU_MAX_CURRENT`, the systemd unit and the network. The
services section prints the GPIO pin map and the exported models the runtime
will use.

### 5. Exported models

`pi.env` sets `SENTRIX_RUNTIME=onnx`. The exports are built on the laptop and
are not in git:

```bash
# on the laptop
python tools/export_models.py --scene          # writes models/exported/ and checks parity
scp -r models/exported pi@<pi>:/tmp/ && ssh pi@<pi> 'sudo cp -r /tmp/exported /opt/sentrix/models/ && sudo chown -R sentrix: /opt/sentrix/models/exported'
```

A missing export falls back to torch (slow on the Pi). `--ncnn` also writes
NCNN exports (needs `ncnn`/`pnnx`). They were measured on the demo Pi on
2026-11-20. In fp16 they are twice as fast but change the person head's
decisions on 4-6 % of frames; in fp32 they are no faster than ONNX. Keep
`SENTRIX_RUNTIME=onnx` (`training/runs/custom_eval/pi5/RUNTIME_2026-11-20.md`).

### 6. Inputs

```bash
sudo systemctl start sentrix && journalctl -u sentrix -f
```

Sign in on `http://<pi>:8000` with the device password and open Device admin →
Hardware (`http://<pi>:8000/admin/hardware`; the old `/node` redirects there),
or read `GET /api/node`:

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
Full table: docs/8 §6.5. The LEDs are the only outputs on the box: no siren
(D25) and no buzzer (D19), so GPIO17 and GPIO26 stay free.

### 8. Microphone (any USB port)

Plug the USB microphone in **while SENTRIX runs**, then move it to another
port (D21). Each time, the Hardware page's microphone row
(`/admin/hardware`) names it within a minute, and `tools/doctor.py` lists it
as the input that would open (it opens none).

### 9. A test recording

From a laptop on the same network, open Device admin → *Record video*
(`http://<pi>:8000/admin/recordings`) and record one minute with someone
talking (D20). Download it on the laptop (a browser on the Pi itself cannot
download): VLC plays the `.avi` with the sound in sync, and the `.json`
sidecar shows few repeated or dropped frames. Then delete it on the device.

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
F1 → XY-3606 buck (5.2 V) → short USB-C → Pi; PC817 mains sense on the
adapter output. Keep the UPS and its cells outside the box. Pass test:

```bash
vcgencmd pmic_read_adc | grep EXT5V   # >= 4.8 V with the modem sending
vcgencmd get_throttled                # 0x0
```

Then 10 mains pulls with no reboot. Measure the UPS runtime (the plan's
≈ 2 h+ is an estimate).

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
(compute p50/p95, dropped clips) on Device admin → Diagnostics (Pipeline),
plus temperature and throttling on the Hardware page. Criteria: docs/8 §7.

### 15. Room calibration (D23)

The scene classifier's thresholds belong to the room and the model: a room it
has never seen can push normal scores over the floors (the demo room raised 18
false Level 3s that way). After a few quiet hours in the room - nobody showing
a weapon or a flame - set the room's own thresholds from the device's log:

```bash
cd /opt/sentrix
sudo .venv/bin/python tools/calibrate_room.py --since 2026-11-20T09:00     # report only
sudo .venv/bin/python tools/calibrate_room.py --since 2026-11-20T09:00 --write
sudo systemctl restart sentrix                                              # the file is read at start-up
```

Read the report before `--write`:
- the replay check must give the device's own decision on every clip;
- the highest moments it lists must not be a staged test (`--exclude
  START,END` drops one);
- each class needs at least 2 h of scored time.

`--write` keeps the old file as `scene.json.bak-<time>` and never lowers a
threshold unless `--allow-lower` is given.

After any new scene model, run the tool again on the new model's quiet hours.
Until then the doctor warns that the calibration was made for other weights.

To check that a staged replica-gun or flame session still reaches Level 3, run
`--check var/events/<that session>.jsonl`.

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

## The demo unit (a user service from the home directory)

The team's demo Pi does not use `install_pi.sh`: the clone lives in
`~/Sentrix` (user `sentrix`), and SENTRIX runs as a systemd **user** service
with lingering on, so it starts at boot without a login:

```bash
mkdir -p ~/.config/systemd/user
cp ~/Sentrix/deploy/pi5/systemd/sentrix-user.service ~/.config/systemd/user/sentrix.service
sudo loginctl enable-linger sentrix
systemctl --user daemon-reload && systemctl --user enable --now sentrix.service
```

Logs: `journalctl --user -u sentrix -f`. Update: `git pull` in `~/Sentrix`, then
`systemctl --user restart sentrix.service`. Room calibration (step 15) runs as
the same user: `cd ~/Sentrix && .venv/bin/python tools/calibrate_room.py
[--write]`, then restart the user service. The demo unit's thresholds since
2026-11-19 are weapon 0.78 and fire 0.75, from its own log.
The web UI is the committed build
of the device app (`sentrix/web/static/app/`), so an update needs no Node.js
on the Pi.

The demo Pi's desktop autostart (`~/.config/autostart/sentrix-dashboard.desktop`,
outside the repository) opens Chromium on `http://127.0.0.1:8000/dashboard`
when a screen is connected. Since the React device app (D22) `/dashboard`
redirects to `/`, the wall view: the live picture and level, taken over by the
alert screen while an SOS request is open. The device admin pages are under
`/admin/` (Diagnostics, Activity, Help requests, Faces, Hardware, Network);
training recordings are made, downloaded and deleted on `/admin/recordings`
from a laptop on the same network (a browser on the Pi itself cannot
download them). Unlike the system service it has no
hardware watchdog; a box that needs one uses `install_pi.sh`.
