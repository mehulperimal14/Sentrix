"""tools/doctor.py hardware checks (T-HW): they render the hardware rows,
degrade to one "skipped" row on a laptop, and never open a webcam when the
camera is a Picamera2 source."""

from __future__ import annotations

import pytest

from tools import doctor


class TestHardwareSection:
    def test_rows_render_with_their_status(self):
        report = doctor.Report()
        doctor.check_hardware(report, rows=[
            {"status": "ok", "name": "platform", "detail": "Raspberry Pi 5 Model B Rev 1.0"},
            {"status": "warn", "name": "PSU_MAX_CURRENT", "detail": "unset"},
            {"status": "fail", "name": "throttling", "detail": "0x50005"},
        ])
        assert report.rows[0] == ("", "── hardware (Raspberry Pi) ", "")
        assert [r[0] for r in report.rows[1:]] == [doctor.OK, doctor.WARN, doctor.FAIL]
        assert report.failures == 1 and report.warnings == 1

    def test_laptop_is_skipped_gracefully(self, monkeypatch):
        from sentrix.io import node
        monkeypatch.setattr(node.sensors, "raspberry_pi_model", lambda root="/": None)
        report = doctor.Report()
        doctor.check_hardware(report)
        assert report.failures == 0 and report.warnings == 0
        assert report.rows[1][1] == "platform" and "skipped" in report.rows[1][2]

    def test_runs_in_main_between_services_and_engines(self):
        import inspect
        source = inspect.getsource(doctor.main)
        assert source.index("check_services") < source.index("check_hardware") \
            < source.index("check_engines")


class TestCameraCheck:
    def test_picamera_source_never_probes_webcams(self, monkeypatch):
        import cv2

        def forbidden(*args, **kwargs):
            pytest.fail("the doctor opened a webcam")

        monkeypatch.setattr(cv2, "VideoCapture", forbidden)
        monkeypatch.setenv("CAMERA_SOURCES", "picamera:0")
        report = doctor.Report()
        doctor.check_camera(report)
        assert report.rows[-1][0] == doctor.OK and "Picamera2" in report.rows[-1][2]

    @pytest.mark.parametrize("spec", [None, "0", "1"])
    def test_an_index_camera_is_not_opened_by_default(self, monkeypatch, spec):
        import cv2
        monkeypatch.setattr(cv2, "VideoCapture",
                            lambda *a, **k: pytest.fail("the doctor opened a webcam"))
        if spec is None:
            monkeypatch.delenv("CAMERA_SOURCES", raising=False)
        else:
            monkeypatch.setenv("CAMERA_SOURCES", spec)
        report = doctor.Report()
        doctor.check_camera(report)
        assert "not opened" in report.rows[-1][2]

    def test_probe_opens_only_the_configured_index(self, monkeypatch):
        import cv2
        opened = []

        class FakeCap:
            def __init__(self, index, *args):
                opened.append(index)

            def isOpened(self):
                return False

            def release(self):
                pass

        monkeypatch.setattr(cv2, "VideoCapture", FakeCap)
        monkeypatch.setenv("CAMERA_SOURCES", "1")
        doctor.check_camera(doctor.Report(), probe=True)
        assert opened == [1]

    def test_start_scripts_never_probe_a_device(self):
        from pathlib import Path
        root = Path(doctor.__file__).resolve().parents[1]
        for script in ("run.sh", "run.bat"):
            text = (root / script).read_text(encoding="utf-8", errors="replace")
            assert "--probe-camera" not in text
