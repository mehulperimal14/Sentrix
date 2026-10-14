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
