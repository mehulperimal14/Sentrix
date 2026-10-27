"""tools/doctor.py rows for the training recordings and the microphone (D20,
D21): whether ffmpeg is there, the recordings against their cap and the disk
floor, and the input the device would open. The doctor only reads: it never
writes, finishes or deletes a recording, and it lists microphones without
opening one."""

from __future__ import annotations

import inspect
import json
from collections import namedtuple
from types import SimpleNamespace

import pytest

from sentrix.config import RecordingConfig
from tools import doctor

Usage = namedtuple("Usage", "total used free")
MB = 1024 * 1024
PLENTY = lambda path: Usage(500_000 * MB, 100_000 * MB, 400_000 * MB)  # noqa: E731
NO_FFMPEG = lambda name: None  # noqa: E731


def _row(report: doctor.Report, name: str) -> tuple[str, str, str]:
    rows = [row for row in report.rows if row[1] == name]
    assert len(rows) == 1, report.rows
    return rows[0]


def _recording(directory, name: str, video_bytes: int = 1000,
               created: str = "2026-10-28T10:00:00+05:30") -> None:
    """A finished recording as the recorder leaves it: video and sidecar."""
    (directory / f"{name}.avi").write_bytes(b"\0" * video_bytes)
    (directory / f"{name}.json").write_text(json.dumps({
        "name": name, "label": "", "created_at": created,
        "seconds": 1.0, "audio": {"present": False}, "interrupted": False}),
        encoding="utf-8")


def _cfg(**overrides) -> RecordingConfig:
    values = dict(enabled=True, max_seconds=600.0, min_free_mb=2048, max_total_mb=20480,
                  keep_days=14.0)
    values.update(overrides)
    return RecordingConfig(**values)


class TestRecordings:
    def test_ffmpeg_on_path_or_not(self, tmp_path):
        report = doctor.Report()
        doctor.check_recordings(report, tmp_path, cfg=_cfg(), which=NO_FFMPEG,
                                disk_usage=PLENTY)
        status, _, detail = _row(report, "ffmpeg")
        assert status == doctor.WARN and ".wav" in detail
        report = doctor.Report()
        doctor.check_recordings(report, tmp_path, cfg=_cfg(),
                                which=lambda name: "/usr/bin/ffmpeg", disk_usage=PLENTY)
        status, _, detail = _row(report, "ffmpeg")
        assert status == doctor.OK and "/usr/bin/ffmpeg" in detail
        assert report.rows[0] == ("", "── training recordings (D20) ", "")

    def test_no_directory_yet(self, tmp_path):
        report = doctor.Report()
        missing = tmp_path / "var" / "recordings"
        doctor.check_recordings(report, missing, cfg=_cfg(), which=NO_FFMPEG,
                                disk_usage=PLENTY)
        status, _, detail = _row(report, "recordings")
        assert status == doctor.OK and "none yet" in detail
        assert _row(report, "free space")[0] == doctor.OK
        assert not missing.exists(), "the doctor never creates the directory"

    def test_count_size_cap_and_crash_leftovers(self, tmp_path):
        _recording(tmp_path, "rec_20261028_100000_gun", 3 * MB)
        _recording(tmp_path, "rec_20261028_110000", 1 * MB)
        (tmp_path / "rec_20261028_120000.part.avi").write_bytes(b"\0" * 10)
        (tmp_path / "rec_20261028_120000.part.json").write_text("{}", encoding="utf-8")
        report = doctor.Report()
        doctor.check_recordings(report, tmp_path, cfg=_cfg(), which=NO_FFMPEG,
                                disk_usage=PLENTY)
        status, _, detail = _row(report, "recordings")
        assert status == doctor.OK
        assert detail.startswith("2 in ") and "4 MB of 20.0 GB allowed" in detail
        assert "deleted after 14 days" in detail
        assert "1 left by a crash" in detail

    def test_at_the_cap_warns(self, tmp_path):
        _recording(tmp_path, "rec_20261028_100000", 2 * MB)
        report = doctor.Report()
        doctor.check_recordings(report, tmp_path, cfg=_cfg(max_total_mb=1), which=NO_FFMPEG,
                                disk_usage=PLENTY)
        status, _, detail = _row(report, "recordings")
        assert status == doctor.WARN and "no new recording starts" in detail

    def test_low_disk_warns(self, tmp_path):
        report = doctor.Report()
        doctor.check_recordings(report, tmp_path, cfg=_cfg(), which=NO_FFMPEG,
                                disk_usage=lambda path: Usage(64_000 * MB, 63_900 * MB, 100 * MB))
        status, _, detail = _row(report, "free space")
        assert status == doctor.WARN and "100 MB" in detail and "will not start" in detail

    def test_recording_off(self, tmp_path):
        report = doctor.Report()
        doctor.check_recordings(report, tmp_path, cfg=_cfg(enabled=False), which=NO_FFMPEG,
                                disk_usage=PLENTY)
        assert "off" in _row(report, "recordings")[2]
        assert not [row for row in report.rows if row[1] == "free space"]

    def test_reads_only(self, tmp_path):
        """A crash's leftovers stay for the app's own recovery; nothing is pruned
        (the recording is far older than keep_days) or written."""
        _recording(tmp_path, "rec_20200101_000000", 10, created="2020-01-01T00:00:00+05:30")
        (tmp_path / "rec_20261028_120000.part.avi").write_bytes(b"\0" * 10)
        before = sorted((p.name, p.stat().st_size) for p in tmp_path.iterdir())
        doctor.check_recordings(doctor.Report(), tmp_path, cfg=_cfg(keep_days=1.0),
                                which=NO_FFMPEG, disk_usage=PLENTY)
        assert sorted((p.name, p.stat().st_size) for p in tmp_path.iterdir()) == before


class FakeSoundDevice:
    """Lists devices; fails the test if anything would open one."""

    def __init__(self, devices, default_input=0, hostapi=0):
        self._devices = devices
        self._default_input = default_input
        self.default = SimpleNamespace(hostapi=hostapi)

    def query_devices(self):
        return [dict(d, index=i) for i, d in enumerate(self._devices)]

    def query_hostapis(self, index):
        return {"name": "MME", "default_input_device": self._default_input}

    def _opened(self, *args, **kwargs):
        pytest.fail("the doctor opened a microphone")

    check_input_settings = InputStream = RawInputStream = rec = _opened


def _device(name, inputs=1, rate=44100.0, hostapi=0):
    return {"name": name, "hostapi": hostapi, "max_input_channels": inputs,
            "default_samplerate": rate}


LAPTOP_AND_USB = [
    _device("Microphone Array (Realtek(R) Audio)", 2, 44100.0),
    _device("HDMI 2 (NVIDIA High Definition Audio)", 2, 48000.0),
    _device("Speakers (Realtek(R) Audio)", 0, 48000.0),
    _device("Microphone (USB PnP Sound Device)", 1, 48000.0),
]


class TestMicrophone:
    def test_usb_input_is_chosen_and_nothing_opened(self):
        report = doctor.Report()
        doctor.check_microphone(report, FakeSoundDevice(LAPTOP_AND_USB), wanted="",
                                enabled=True)
        status, _, detail = _row(report, "microphone")
        assert status == doctor.OK
        assert "[3] Microphone (USB PnP Sound Device)" in detail and "usb" in detail
        assert "48000 Hz" in detail
        assert "1 other input(s)" in detail, "HDMI and outputs are not inputs it would open"
        assert report.warnings == 0 and report.rows[0][1].startswith("── microphone")

    def test_no_input_warns(self):
        report = doctor.Report()
        doctor.check_microphone(report, FakeSoundDevice([_device("HDMI 0 (vc4-hdmi-0)", 2)],
                                                        default_input=-1),
                                wanted="", enabled=True)
        status, _, detail = _row(report, "microphone")
        assert status == doctor.WARN and "any port" in detail

    def test_mic_device_by_index_and_unmatched(self):
        report = doctor.Report()
        doctor.check_microphone(report, FakeSoundDevice(LAPTOP_AND_USB), wanted="0",
                                enabled=True)
        assert "[0] Microphone Array" in _row(report, "microphone")[2]
        assert "asked for" in _row(report, "microphone")[2] and report.warnings == 0

        report = doctor.Report()
        doctor.check_microphone(report, FakeSoundDevice(LAPTOP_AND_USB), wanted="Scalebee",
                                enabled=True)
        status, _, detail = _row(report, "SENTRIX_MIC_DEVICE")
        assert status == doctor.WARN and "'Scalebee'" in detail
        assert "USB PnP" in _row(report, "microphone")[2], "falls back to automatic"

    def test_audio_off(self):
        report = doctor.Report()
        doctor.check_microphone(report, FakeSoundDevice(LAPTOP_AND_USB), enabled=False)
        assert _row(report, "microphone") == (doctor.OK, "microphone", "off (SENTRIX_AUDIO=0)")

    def test_sounddevice_missing(self, monkeypatch):
        from sentrix.perception import audio
        monkeypatch.setattr(audio, "sd", None)
        report = doctor.Report()
        doctor.check_microphone(report, wanted="", enabled=True)
        status, _, detail = _row(report, "microphone")
        assert status == doctor.WARN and "sounddevice" in detail

    def test_listing_failure_warns(self):
        class Broken(FakeSoundDevice):
            def query_devices(self):
                raise RuntimeError("PortAudio not initialized")

        report = doctor.Report()
        doctor.check_microphone(report, Broken([]), wanted="", enabled=True)
        status, _, detail = _row(report, "microphone")
        assert status == doctor.WARN and "cannot list" in detail


class TestMain:
    def test_rows_run_after_exports_and_before_engines(self):
        source = inspect.getsource(doctor.main)
        assert source.index("check_exports") < source.index("check_recordings") \
            < source.index("check_microphone") < source.index("check_engines")
