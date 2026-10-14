"""Pi deployment files (T-HW): run.sh's Pi path, install_pi.sh, the .env merge,
and the systemd / config.txt / NetworkManager / ufw templates.

The shell scripts run only in their dry-run modes, against a fake device-tree
model file; nothing is installed and no command with side effects runs.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sentrix.paths import ROOT_DIR

PI5 = "Raspberry Pi 5 Model B Rev 1.0"
DEPLOY = ROOT_DIR / "deploy" / "pi5"


def _bash() -> str | None:
    """A POSIX bash; never Windows' WSL launcher (System32/WindowsApps)."""
    candidates = [shutil.which("bash")]
    if sys.platform == "win32":
        candidates += [r"C:\Program Files\Git\bin\bash.exe",
                       r"C:\Program Files\Git\usr\bin\bash.exe"]
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        lowered = candidate.lower()
        if "system32" in lowered or "windowsapps" in lowered:
            continue
        return candidate
    return None


BASH = _bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="no POSIX bash on this machine")


def _model_file(tmp_path, model: str) -> str:
    path = tmp_path / "model"
    path.write_bytes(model.encode() + b"\x00")
    return path.as_posix()


def _run(script: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run([BASH, script.as_posix(), *args], cwd=ROOT_DIR, env=full_env,
                          capture_output=True, text=True, timeout=120)


# ── run.sh ────────────────────────────────────────────────────────────────────
class TestRunSh:
    @needs_bash
    def test_pi_uses_system_site_packages_and_requirements_pi(self, tmp_path):
        done = _run(ROOT_DIR / "run.sh", env={"SENTRIX_RUN_DRY_RUN": "1",
                                              "SENTRIX_PI_MODEL_FILE": _model_file(tmp_path, PI5)})
        assert done.returncode == 0, done.stderr
        assert done.stdout.splitlines() == [f"platform=pi ({PI5})",
                                            "venv_args=--system-site-packages",
                                            "requirements=requirements-pi.txt"]

    @needs_bash
    def test_laptop_behaviour_is_unchanged(self, tmp_path):
        missing = (tmp_path / "no-such-file").as_posix()
        for model_file in (missing, _model_file(tmp_path, "Dell Inc. XPS 15")):
            done = _run(ROOT_DIR / "run.sh", env={"SENTRIX_RUN_DRY_RUN": "1",
                                                  "SENTRIX_PI_MODEL_FILE": model_file})
            assert done.returncode == 0, done.stderr
            assert done.stdout.splitlines() == ["platform=other", "venv_args=none",
                                                "requirements=requirements.txt"]

    def test_laptop_steps_kept(self):
        text = (ROOT_DIR / "run.sh").read_text(encoding="utf-8")
        assert "python3 -m venv $VENV_ARGS .venv" in text
        for step in ("[1/5]", "[2/5]", "[3/5]", "[4/5]", "[5/5]", "tools/setup_env.py",
                     "tools/doctor.py", 'exec "$PY" -m sentrix'):
            assert step in text, step
        # The OpenCV swap runs on the Pi only.
        assert '[ "$ON_PI" = 1 ] && "$PY" -m pip show opencv-python' in text

    def test_lf_line_endings(self):
        for script in (ROOT_DIR / "run.sh", DEPLOY / "install_pi.sh", DEPLOY / "ufw.sh"):
            assert b"\r\n" not in script.read_bytes(), f"{script.name} must stay LF"


# ── install_pi.sh ─────────────────────────────────────────────────────────────
class TestInstallPi:
    @needs_bash
    def test_dry_run_plan(self, tmp_path):
        done = _run(DEPLOY / "install_pi.sh", "--dry-run", "--boot-config", "--ufw",
                    "10.0.0.0/24", "--modem-apn", "airtelgprs.com", "--modem-ecm",
                    env={"SENTRIX_PI_MODEL_FILE": _model_file(tmp_path, PI5)})
        assert done.returncode == 0, done.stderr
        out = done.stdout
        apt = next(line for line in out.splitlines() if "apt-get install" in line
                   and "python3-picamera2" in line)
        for package in ("python3-picamera2", "python3-lgpio", "libportaudio2", "python3-venv"):
            assert package in apt, package
        assert "+ python3 -m venv --system-site-packages .venv" in out
        assert "pip install -r requirements-pi.txt" in out
        assert "pip uninstall -y opencv-python" in out
        assert 'opencv-python-headless>=4.10.0,<5' in out
        assert "merge_env.py .env deploy/pi5/pi.env" in out
        for dest in ("/etc/systemd/system/sentrix.service",
                     "/etc/systemd/journald.conf.d/10-sentrix.conf",
                     "/etc/systemd/system.conf.d/10-sentrix-watchdog.conf",
                     "/etc/NetworkManager/conf.d/20-sentrix-connectivity.conf",
                     "/etc/polkit-1/rules.d/50-sentrix-networkmanager.rules",
                     "/etc/NetworkManager/system-connections/sentrix-modem.nmconnection",
                     "/etc/NetworkManager/system-connections/sentrix-modem-ecm.nmconnection"):
            assert f"-> {dest}" in out, dest
        assert "+ systemctl enable sentrix.service" in out
        assert "gpio=17=op,dl" in out, "NPN driver: drive low at boot"
        assert "ufw.sh 10.0.0.0/24" in out
        assert "LED in place of the siren" in out

    @needs_bash
    def test_refuses_anything_but_a_pi(self, tmp_path):
        done = _run(DEPLOY / "install_pi.sh", "--dry-run",
                    env={"SENTRIX_PI_MODEL_FILE": _model_file(tmp_path, "Generic x86")})
        assert done.returncode == 1 and "not a Raspberry Pi" in done.stderr

    @needs_bash
    def test_unknown_option(self, tmp_path):
        done = _run(DEPLOY / "install_pi.sh", "--frobnicate",
                    env={"SENTRIX_PI_MODEL_FILE": _model_file(tmp_path, PI5)})
        assert done.returncode == 2

    def test_every_template_placeholder_is_rendered(self):
        script = (DEPLOY / "install_pi.sh").read_text(encoding="utf-8")
        rendered = set(re.findall(r'-e "s\|@([A-Z_]+)@\|', script))
        assert rendered == {"APP_DIR", "USER", "SIREN_PIN", "SIREN_OFF", "APN", "IFNAME"}
        installed = re.findall(r'install_file "\$HERE/([^"]+)"', script)
        assert installed, "install_pi.sh installs templates"
        for relative in installed:
            text = (DEPLOY / relative).read_text(encoding="utf-8")
            left = set(re.findall(r"@([A-Z_]+)@", text)) - rendered
            assert not left, f"{relative}: {left} never filled"


# ── .env merge ────────────────────────────────────────────────────────────────
def _merge_env():
    spec = importlib.util.spec_from_file_location("merge_env", DEPLOY / "merge_env.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMergeEnv:
    def test_existing_values_are_kept_and_missing_keys_appended(self):
        merge_env = _merge_env()
        defaults = merge_env.parse_defaults((DEPLOY / "pi.env").read_text(encoding="utf-8"))
        assert defaults["CAMERA_SOURCES"] == "picamera:0"
        assert defaults["SENTRIX_GPIO_ENABLED"] == "1" and defaults["SENTRIX_NODE_ENABLED"] == "1"
        text, changed = merge_env.merge("CAMERA_SOURCES=0\nSENTRIX_PASSWORD=secret\n", defaults)
        assert "CAMERA_SOURCES=0\n" in text, "a configured value is never changed"
        assert "CAMERA_SOURCES" not in changed and "SENTRIX_PROFILE" in changed
        assert merge_env.HEADING in text and "SENTRIX_PASSWORD=secret" in text
        again, changed_again = merge_env.merge(text, defaults)
        assert changed_again == [] and again == text, "idempotent"

    def test_fresh_env_takes_the_pi_defaults(self):
        merge_env = _merge_env()
        template = (ROOT_DIR / ".env.example").read_text(encoding="utf-8")
        defaults = merge_env.parse_defaults((DEPLOY / "pi.env").read_text(encoding="utf-8"))
        text, changed = merge_env.merge(template, defaults, fresh=True)
        assert re.search(r"(?m)^CAMERA_SOURCES=picamera:0$", text)
        assert re.search(r"(?m)^SENTRIX_SIREN=gpio$", text)
        assert not re.search(r"(?m)^CAMERA_SOURCES=0$", text)
        for key in ("SENTRIX_PASSWORD", "SESSION_SECRET", "EVIDENCE_AES_KEY"):
            assert f"\n{key}=\n" in text, "secrets stay for tools/setup_env.py"

    def test_pi_env_is_systemd_and_dotenv_safe(self):
        for line in (DEPLOY / "pi.env").read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                assert re.fullmatch(r"[A-Z_][A-Z0-9_]*=[^\s#]*", line), line

    def test_command_line(self, tmp_path):
        merge_env = _merge_env()
        env = tmp_path / ".env"
        env.write_text("CAMERA_SOURCES=0\n")
        assert merge_env.main([str(env), str(DEPLOY / "pi.env")]) == 0
        assert "SENTRIX_PROFILE=pi5-clip" in env.read_text()
        assert merge_env.main([str(env)]) == 2


# ── templates ─────────────────────────────────────────────────────────────────
class TestTemplates:
    def test_systemd_unit(self):
        unit = (DEPLOY / "systemd" / "sentrix.service.in").read_text(encoding="utf-8")
        for line in ("Type=notify", "NotifyAccess=main", "Restart=always", "WatchdogSec=30",
                     "StartLimitIntervalSec=0", "User=@USER@",
                     "ExecStart=@APP_DIR@/.venv/bin/python -m sentrix",
                     "ExecStartPre=-+/usr/bin/pinctrl set @SIREN_PIN@ op @SIREN_OFF@",
                     "ExecStopPost=-+/usr/bin/pinctrl set @SIREN_PIN@ op @SIREN_OFF@",
                     "ReadWritePaths=@APP_DIR@/var -/etc/sentrix"):
            assert line in unit, line
        assert "EnvironmentFile" not in unit.split("[Service]")[1].split("# ")[0], \
            "the app reads .env itself (inline comments)"

    def test_journald_and_watchdog_drop_ins(self):
        journald = (DEPLOY / "systemd" / "10-sentrix-journald.conf").read_text(encoding="utf-8")
        assert "[Journal]" in journald and "SystemMaxUse=200M" in journald
        assert "SystemKeepFree=" in journald and "MaxRetentionSec=" in journald
        watchdog = (DEPLOY / "systemd" / "10-sentrix-watchdog.conf").read_text(encoding="utf-8")
        assert "[Manager]" in watchdog and "RuntimeWatchdogSec=10s" in watchdog

    def test_config_txt_snippet(self):
        lines = [l.strip() for l in (DEPLOY / "config.txt.snippet").read_text(
            encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
        for setting in ("gpio=17=op,dl", "dtparam=rtc_bbat_vchg=3000000",
                        "kernel_watchdog_timeout=10", "camera_auto_detect=1",
                        "dtparam=i2c_arm=on"):
            assert setting in lines, setting
        text = (DEPLOY / "config.txt.snippet").read_text(encoding="utf-8")
        assert "PSU_MAX_CURRENT=5000" in text and "rpi-eeprom-config --edit" in text
        assert text.startswith("# >>> SENTRIX >>>") and "# <<< SENTRIX <<<" in text

    def test_failover_metrics_and_connectivity_check(self):
        nm = DEPLOY / "networkmanager"

        def metrics(name):
            return re.findall(r"(?m)^route-metric=(\d+)$", (nm / name).read_text(encoding="utf-8"))

        assert metrics("sentrix-wifi.nmconnection.in") == ["600", "600"]
        assert metrics("sentrix-modem-gsm.nmconnection.in") == ["700", "700"]
        assert metrics("sentrix-modem-ecm.nmconnection.in") == ["700", "700"]
        connectivity = (nm / "20-sentrix-connectivity.conf").read_text(encoding="utf-8")
        assert "[connectivity]" in connectivity and re.search(r"(?m)^uri=http", connectivity)
        hotspot = (nm / "sentrix-setup-hotspot.nmconnection.in").read_text(encoding="utf-8")
        assert "mode=ap" in hotspot and "autoconnect=false" in hotspot
        from sentrix.io import network
        assert (network.WIFI_ROUTE_METRIC, network.MODEM_ROUTE_METRIC) == (600, 700)

    def test_ufw_is_lan_only(self):
        script = (DEPLOY / "ufw.sh").read_text(encoding="utf-8")
        assert "ufw default deny incoming" in script
        allows = [l for l in script.splitlines() if l.startswith("ufw allow")]
        assert allows and all(" from " in l or " in on wlan0 " in l for l in allows), \
            "every inbound rule is scoped to the LAN or the setup hotspot"
        assert "--force reset" not in script

    def test_polkit_rule_is_scoped_to_the_service_user(self):
        rule = (DEPLOY / "polkit" / "50-sentrix-networkmanager.rules").read_text(encoding="utf-8")
        assert 'subject.user !== "@USER@"' in rule
        assert "org.freedesktop.NetworkManager.settings.modify.system" in rule

    def test_readme_has_the_relay_test(self):
        readme = (DEPLOY / "README.md").read_text(encoding="utf-8")
        assert "## Bring-up order" in readme and "LED in place of the siren" in readme
        for event in ("Cold start", "Reboot", "Service stop", "kill -9", "UPS"):
            assert event in readme, event

    def test_requirements_pi_pins_headless_opencv(self):
        text = (ROOT_DIR / "requirements-pi.txt").read_text(encoding="utf-8")
        requirements = [l.split("#")[0].strip() for l in text.splitlines()
                        if l.strip() and not l.lstrip().startswith("#")]
        assert "opencv-python-headless>=4.10.0,<5" in requirements
        assert not any(r.startswith("opencv-python>") or r == "opencv-python"
                       for r in requirements)
        assert "gpiozero>=2.0.1" in requirements and not any("RPi.GPIO" in r for r in requirements)
