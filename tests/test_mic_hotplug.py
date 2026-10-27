"""A USB microphone plugged into any port at any time is picked up without
restarting SENTRIX, on the Pi (ALSA names, ``/proc/asound``) and on the
Windows laptop (MME names, a built-in microphone already open): the best
input is chosen (asked for > USB, a newer one first > another input plugged in
while running > default; never HDMI or a loopback unless asked for; a headset
or a virtual microphone only as a last resort), opened at a rate it takes (on Linux's plughw at
the device's own rate) and resampled to 16 kHz, and PortAudio's device list
is rebuilt only when the system's own list changes, never while a stream is
still open.

No real audio device is touched: ``sounddevice`` is replaced by a fake whose
device list changes only after ``_terminate()`` / ``_initialize()``, as a
real USB hot-plug does, and the system's device fingerprint is the fake's
list of plugged devices. The fake records every ``_terminate()`` that finds a
stream still open (real PortAudio frees it, and the later close is a
use-after-free); every test using the ``engines`` fixture ends with none.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pytest

import sentrix.perception.audio as A
from sentrix.config import settings
from sentrix.pipeline.state import SystemState, mic_listening

RATE = 16000
BUILTIN = "Microphone Array (Realtek(R) Audio)"
USB = "Microphone (USB PnP Sound Device)"
PI_USB = "USB PnP Sound Device: Audio (hw:2,0)"


class FakeDevice:
    def __init__(self, name, *, rates=(16000, 32000, 44100, 48000), channels=1,
                 layouts=None, default_rate=48000.0, hostapi=0):
        self.name = name
        self.rates = tuple(rates)
        self.channels = channels
        self.layouts = tuple(layouts) if layouts else tuple(range(1, channels + 1))
        self.default_rate = default_rate
        self.hostapi = hostapi
        self.busy = False          # opening fails: another program holds it
        self.hidden = False        # PortAudio could not open it while listing


class FakeStream:
    def __init__(self, sd, device, samplerate, channels, blocksize, callback):
        self.sd, self.device = sd, device
        self.rate, self.channels, self.blocksize = int(samplerate), channels, blocksize
        self.callback = callback
        self.active = self.closed = self.aborted = False

    def start(self):
        self.active = True
        self.sd.streams.append(self)

    def abort(self):
        if self.sd.hang is not None:          # a driver stuck on a vanished device
            self.sd.hang.wait(10)
        self.active = False
        self.aborted = True

    def stop(self):
        self.active = False

    def close(self):
        self.active = False
        self.closed = True

    def feed(self, signal=None, seconds: float = 1.0, status=None):
        """One block at the stream's own rate and channel count."""
        if signal is None:
            signal = np.zeros((int(self.rate * seconds), self.channels), np.float32)
        signal = np.asarray(signal, np.float32)
        if signal.ndim == 1:
            signal = np.repeat(signal[:, None], self.channels, axis=1)
        info = SimpleNamespace(inputBufferAdcTime=0.0, currentTime=0.0)
        self.callback(signal, len(signal), info, status)


class FakeSoundDevice:
    """PortAudio as sounddevice shows it: what is plugged in reaches the
    device list only when PortAudio re-initialises."""

    def __init__(self):
        self.plugged: list[FakeDevice] = []       # physically connected
        self.visible: list[FakeDevice] = []       # listed at the last initialisation
        self._initialized = 1
        self.inits = self.terminates = 0
        self.fail_terminate = self.fail_initialize = False
        self.default = SimpleNamespace(hostapi=0)
        self.default_input: str | None = None
        self.streams: list[FakeStream] = []
        self.hang: threading.Event | None = None
        #: Streams still open when PortAudio was terminated, per terminate.
        self.violations: list[list[str]] = []

    def fingerprint(self):
        return "|".join(d.name for d in self.plugged)

    def boot(self, *devices, default=None):
        """Devices present when SENTRIX starts (listed at sounddevice's import)."""
        self.plugged = list(devices)
        self.visible = [d for d in devices if not d.hidden]
        self.default_input = default
        A._listed_fingerprint = self.fingerprint()

    def _terminate(self):
        self.terminates += 1
        open_streams = [s.device.name for s in self.streams if not s.closed]
        if open_streams:                          # Pa_Terminate frees them under their owner
            self.violations.append(open_streams)
        if self.fail_terminate:
            raise RuntimeError("Error terminating PortAudio")
        self._initialized -= 1
        for stream in self.streams:               # Pa_Terminate closes every stream
            stream.active = False

    def _initialize(self):
        self.inits += 1
        if self.fail_initialize:
            raise RuntimeError("Error initializing PortAudio")
        self.visible = [d for d in self.plugged if not d.hidden]
        self._initialized += 1

    def query_devices(self, device=None, kind=None):
        if self._initialized <= 0:
            raise RuntimeError("PortAudio not initialized")
        return [{"name": d.name, "index": i, "hostapi": d.hostapi,
                 "max_input_channels": d.channels, "max_output_channels": 0,
                 "default_samplerate": d.default_rate} for i, d in enumerate(self.visible)]

    def query_hostapis(self, index=None):
        names = [d.name for d in self.visible]
        default = names.index(self.default_input) if self.default_input in names else -1
        return {"name": "MME" if index in (0, None) else "WASAPI",
                "default_input_device": default}

    def _device(self, index, samplerate, channels):
        if self._initialized <= 0:
            raise RuntimeError("PortAudio not initialized")
        device = self.visible[index]
        if device not in self.plugged:
            raise RuntimeError("Device unavailable [PaErrorCode -9985]")
        if device.busy:
            raise RuntimeError("Device unavailable: busy [PaErrorCode -9985]")
        if int(samplerate) not in device.rates:
            raise RuntimeError("Invalid sample rate [PaErrorCode -9997]")
        if channels not in device.layouts:
            raise RuntimeError("Invalid number of channels [PaErrorCode -9998]")
        return device

    def check_input_settings(self, device=None, channels=None, dtype=None,
                             extra_settings=None, samplerate=None):
        self._device(device, samplerate, channels)

    def InputStream(self, device=None, samplerate=None, channels=None, dtype=None,
                    blocksize=None, callback=None, **_):
        found = self._device(device, samplerate, channels)
        return FakeStream(self, found, samplerate, channels, blocksize, callback)


@pytest.fixture
def fake_sd(monkeypatch):
    sd = FakeSoundDevice()
    monkeypatch.setattr(A, "sd", sd)
    monkeypatch.setattr(A, "_SD", True)
    monkeypatch.setattr(A, "devices_fingerprint", sd.fingerprint)
    monkeypatch.setattr(A, "_listed_fingerprint", sd.fingerprint())
    monkeypatch.setattr(A, "settings", replace(settings, audio=replace(
        settings.audio, model="off", mic_retry_seconds=0.5, mic_stall_seconds=1.0,
        mic_device="", mic_rescan_seconds=60.0)))
    return sd


@pytest.fixture
def engines(fake_sd):
    """``make(watch=False)``: a capturing engine on the fake; stopped afterwards.
    Without the watcher thread the test drives ``_watch_once`` itself."""
    made = []

    def make(watch: bool = False):
        engine = A.AudioEngine(capture=False)
        engine._start_capture(watch=watch)
        made.append(engine)
        return engine

    yield make
    if fake_sd.hang is not None:
        fake_sd.hang.set()
    for engine in made:
        engine.stop()
    assert fake_sd.violations == [], "PortAudio terminated with a stream still open"


@pytest.fixture
def records():
    """Log records of the audio logger (``sentrix`` does not propagate)."""
    got: list[logging.LogRecord] = []
    handler = logging.Handler(logging.DEBUG)
    handler.emit = got.append
    A.log.addHandler(handler)
    yield got
    A.log.removeHandler(handler)


def use(monkeypatch, **audio):
    monkeypatch.setattr(A, "settings",
                        replace(A.settings, audio=replace(A.settings.audio, **audio)))


def listen(engine):
    heard = []
    engine.add_sample_listener(lambda samples, end_at: heard.append((samples.copy(), end_at)))
    return heard


def _wait(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def tone(freq, seconds, rate, amplitude=0.5):
    t = np.arange(int(seconds * rate)) / rate
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


# ── choosing the microphone ───────────────────────────────────────────────────
def test_usb_microphone_is_chosen_over_the_built_in_one(fake_sd, engines):
    fake_sd.boot(FakeDevice(BUILTIN, channels=2), FakeDevice(USB), default=BUILTIN)
    engine = engines()
    assert [s.device.name for s in fake_sd.streams] == [USB]
    assert engine.status()["candidates"] == [USB, BUILTIN]


def test_microphone_plugged_in_while_the_built_in_one_is_open_is_switched_to(fake_sd, engines):
    builtin = FakeDevice(BUILTIN, channels=2)
    fake_sd.boot(builtin, default=BUILTIN)
    engine = engines()
    first = fake_sd.streams[-1]
    assert first.device is builtin
    first.feed()
    assert _wait(lambda: engine.available)

    fake_sd.plugged.append(FakeDevice(USB))          # the laptop: plug the USB mic in
    engine._watch_once()
    assert first.aborted and first.closed
    assert fake_sd.inits == 1                        # PortAudio listed the devices again
    assert fake_sd.violations == [], "closed before PortAudio was re-initialised"
    second = fake_sd.streams[-1]
    assert second.device.name == USB and not engine.available
    second.feed()
    assert _wait(lambda: engine.available and engine.status()["device"] == USB)


@pytest.mark.parametrize("wanted", ["realtek", "0"])
def test_the_microphone_asked_for_wins(fake_sd, engines, monkeypatch, wanted):
    use(monkeypatch, mic_device=wanted)              # SENTRIX_MIC_DEVICE: name part or index
    fake_sd.boot(FakeDevice(BUILTIN), FakeDevice(USB), default=BUILTIN)
    engine = engines()
    assert fake_sd.streams[-1].device.name == BUILTIN
    assert engine.status()["candidates"][0] == BUILTIN


def test_a_microphone_asked_for_but_missing_falls_back_with_one_warning(fake_sd, engines,
                                                                       monkeypatch, records):
    use(monkeypatch, mic_device="Blue Yeti")
    fake_sd.boot(FakeDevice(USB))
    engine = engines()
    for _ in range(3):                            # every later look finds it missing too
        with A._PA_LOCK:
            engine._scan()
    assert fake_sd.streams[-1].device.name == USB
    assert sum("Blue Yeti" in r.getMessage() for r in records
               if r.levelno == logging.WARNING) == 1


def test_hdmi_monitor_and_loopback_inputs_are_never_chosen(fake_sd, engines):
    fake_sd.boot(FakeDevice("vc4-hdmi-0: MAI PCM i2s-hifi-0 (hw:0,0)", channels=2),
                 FakeDevice("Monitor of Built-in Audio Analog Stereo", channels=2),
                 FakeDevice("Stereo Mix (Realtek(R) Audio)", channels=2),
                 FakeDevice("Loopback: PCM (hw:1,0)", channels=2))
    engine = engines()
    assert fake_sd.streams == []
    status = engine.status()
    assert status["candidates"] == [] and status["available"] is False
    assert status["last_error"] == "no input device found"


def test_rank_inputs_scores_and_filters():
    devices = [
        {"name": "Microsoft Sound Mapper - Input", "hostapi": 0, "max_input_channels": 2},
        {"name": BUILTIN, "hostapi": 0, "max_input_channels": 2},
        {"name": "Speakers (Realtek(R) Audio)", "hostapi": 0, "max_input_channels": 0},
        {"name": USB, "hostapi": 0, "max_input_channels": 1},
        {"name": USB, "hostapi": 2, "max_input_channels": 1},       # the same mic, WASAPI
        {"name": "Stereo Mix (Realtek(R) Audio)", "hostapi": 0, "max_input_channels": 2},
        {"name": "Headset (WH-1000XM4 Hands-Free ", "hostapi": 0, "max_input_channels": 1},
        {"name": "Microphone (NVIDIA Broadcast)", "hostapi": 0, "max_input_channels": 1},
        {"name": "External Microphone (Realtek(R) Audio)", "hostapi": 0,
         "max_input_channels": 2},
        {"name": "Microphone (USB Audio Device)", "hostapi": 0, "max_input_channels": 1},
    ]
    later = {"Headset (WH-1000XM4 Hands-Free ", "Microphone (NVIDIA Broadcast)",
             "External Microphone (Realtek(R) Audio)", "Microphone (USB Audio Device)"}
    known = {d["name"] for d in devices} - later
    ranked = A.rank_inputs(devices, hostapi=0, default_input=1, known=known)
    assert [(c.name, c.score) for c in ranked] == [
        ("Microphone (USB Audio Device)", 300), (USB, 200),
        ("External Microphone (Realtek(R) Audio)", 100), (BUILTIN, 10),
        ("Microsoft Sound Mapper - Input", 0),
        ("Headset (WH-1000XM4 Hands-Free ", -900), ("Microphone (NVIDIA Broadcast)", -900)], \
        "a newer USB mic first; no other input above a USB one; headsets and virtual mics last"
    assert ranked[0].reasons == ("plugged in", "usb")
    # asked for by index: any host API, and a loopback or headset only when asked for
    assert A.rank_inputs(devices, hostapi=0, wanted="4")[0].hostapi == 2
    assert A.rank_inputs(devices, hostapi=0, wanted="stereo mix")[0].score == 1000
    assert A.rank_inputs(devices, hostapi=0, wanted="hands-free")[0].name.startswith("Headset")


@pytest.mark.parametrize("name", [
    "Headset (WH-1000XM4 Hands-Free AG Audio)", "Headset Microphone (Jabra Evolve 65)",
    "Microphone (AirPods Pro)", "bluez_input.40_72_18 (Bluetooth)",
    "CABLE Output (VB-Audio Virtual Cable)", "Microphone (NVIDIA Broadcast)",
    "Microphone (Krisp Microphone)", "Microphone (Microsoft Teams Audio)",
    "Microphone (ZoomAudioDevice)", "Voicemeeter Out B1 (VB-Audio Voicemeeter VAIO)"])
def test_inputs_that_may_not_hear_the_room_come_last(name):
    devices = [{"name": name, "hostapi": 0, "max_input_channels": 1},
               {"name": BUILTIN, "hostapi": 0, "max_input_channels": 2}]
    ranked = A.rank_inputs(devices, hostapi=0, known=set())
    assert [c.name for c in ranked] == [BUILTIN, name], "any real microphone first"
    assert ranked[-1].reasons[-1] == "last resort" and ranked[-1].score < 0


def test_a_usb_adapter_calling_itself_a_headset_is_used_when_it_is_the_only_one():
    """On a Pi the USB microphone is the only input; a cheap adapter may be
    named like a headset, and must still be opened."""
    devices = [{"name": "USB Headset: Audio (hw:2,0)", "hostapi": 0, "max_input_channels": 1}]
    ranked = A.rank_inputs(devices, hostapi=0, known=set())
    assert [c.name for c in ranked] == ["USB Headset: Audio (hw:2,0)"]


def test_a_bluetooth_headset_connecting_does_not_take_over_the_usb_microphone(fake_sd,
                                                                             engines):
    fake_sd.boot(FakeDevice(BUILTIN, channels=2), FakeDevice(USB), default=BUILTIN)
    engine = engines()
    fake_sd.streams[-1].feed()
    assert _wait(lambda: engine.available and engine.status()["device"] == USB)
    fake_sd.plugged.append(FakeDevice("Headset (WH-1000XM4 Hands-Free "))   # earbuds connect
    engine._watch_once()
    assert fake_sd.streams[-1].device.name == USB, "the room's microphone stays"
    assert engine.status()["candidates"][-1] == "Headset (WH-1000XM4 Hands-Free ", "last"


def test_a_bluetooth_headset_does_not_take_over_the_built_in_microphone(fake_sd, engines):
    fake_sd.boot(FakeDevice(BUILTIN, channels=2), default=BUILTIN)
    engine = engines()
    fake_sd.streams[-1].feed()
    assert _wait(lambda: engine.available)
    fake_sd.plugged.append(FakeDevice("Headset (WH-1000XM4 Hands-Free "))
    engine._watch_once()
    assert fake_sd.streams[-1].device.name == BUILTIN


def test_usb_on_linux_is_read_from_the_sound_card(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_LINUX", True)
    monkeypatch.setattr(A, "_ASOUND", tmp_path)
    (tmp_path / "card2").mkdir()
    (tmp_path / "card2" / "usbid").write_text("0d8c:0014\n")
    (tmp_path / "card0").mkdir()
    assert A.is_usb("C-Media Audio Adapter: Audio (plughw:2,0)")
    assert A.is_usb("Generic Mic: Audio (hw:2,0)")
    assert not A.is_usb("bcm2835 Headphones: - (hw:0,0)")
    assert not A.is_usb("default")


def test_device_fingerprint_opens_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_LINUX", True)
    monkeypatch.setattr(A, "_ASOUND", tmp_path)
    assert A.devices_fingerprint() is None                  # no /proc/asound/cards
    (tmp_path / "cards").write_text(" 0 [vc4hdmi0 ]: vc4-hdmi - vc4-hdmi-0\n")
    assert "vc4hdmi0" in A.devices_fingerprint()
    monkeypatch.setattr(A, "_LINUX", False)
    monkeypatch.setattr(A, "_WINDOWS", True)
    winmm = SimpleNamespace(waveInGetNumDevs=lambda: 3)
    monkeypatch.setitem(sys.modules, "ctypes", SimpleNamespace(windll=SimpleNamespace(winmm=winmm)))
    assert A.devices_fingerprint() == "3"
    monkeypatch.setattr(A, "_WINDOWS", False)
    assert A.devices_fingerprint() is None


# ── rates, channels and resampling ────────────────────────────────────────────
def test_a_48khz_only_microphone_is_resampled_to_16khz(fake_sd, engines):
    fake_sd.boot(FakeDevice(PI_USB, rates=(48000,), default_rate=48000.0))
    engine = engines()
    stream = fake_sd.streams[-1]
    assert (stream.rate, stream.blocksize) == (48000, 48000)
    heard = listen(engine)
    before = time.monotonic()
    for block in np.split(tone(1000, 3.0, 48000), 3):
        stream.feed(block)
    after = time.monotonic()
    assert _wait(lambda: len(heard) == 3)
    assert [len(s) for s, _ in heard] == [RATE] * 3        # 1 s blocks at 16 kHz
    assert all(before - 0.01 <= end_at <= after for _, end_at in heard)
    out = np.concatenate([s for s, _ in heard])[200:]       # past the filter's start
    assert np.sqrt(np.mean(out ** 2)) == pytest.approx(0.5 / np.sqrt(2), rel=0.02)
    peak = np.argmax(np.abs(np.fft.rfft(out))) * RATE / len(out)
    assert peak == pytest.approx(1000, abs=2)
    assert engine.status()["rate"] == 48000


def test_a_44khz_only_microphone_is_resampled_to_16khz(fake_sd, engines):
    fake_sd.boot(FakeDevice(PI_USB, rates=(44100,), default_rate=44100.0))
    engine = engines()
    stream = fake_sd.streams[-1]
    assert stream.rate == 44100
    heard = listen(engine)
    for block in np.split(tone(440, 3.0, 44100), 3):
        stream.feed(block)
    assert _wait(lambda: len(heard) == 3)
    assert [len(s) for s, _ in heard] == [RATE] * 3
    out = np.concatenate([s for s, _ in heard])[400:]       # past 10 ms of history
    assert np.sqrt(np.mean(out ** 2)) == pytest.approx(0.5 / np.sqrt(2), rel=0.02)
    peak = np.argmax(np.abs(np.fft.rfft(out))) * RATE / len(out)
    assert peak == pytest.approx(440, abs=2)


def test_a_stereo_only_microphone_is_downmixed(fake_sd, engines):
    fake_sd.boot(FakeDevice(USB, channels=2, layouts=(2,), rates=(16000,)))
    engine = engines()
    stream = fake_sd.streams[-1]
    assert stream.channels == 2
    heard = listen(engine)
    block = np.empty((RATE, 2), np.float32)
    block[:, 0], block[:, 1] = 0.2, 0.6
    stream.feed(block)
    assert _wait(lambda: heard)
    assert heard[0][0].shape == (RATE,) and np.allclose(heard[0][0], 0.4)


def test_on_plughw_the_microphone_opens_at_its_own_rate(fake_sd, engines, monkeypatch):
    # plughw accepts 16 kHz from any card and converts with ALSA's linear
    # converter (no anti-alias filter without speexrate): 8-24 kHz would fold
    # into the band. The device's own rate first; our filter does the rest.
    monkeypatch.setattr(A, "_PLUGHW", True)
    fake_sd.boot(FakeDevice(PI_USB, rates=(16000, 44100, 48000), default_rate=48000.0))
    engine = engines()
    stream = fake_sd.streams[-1]
    assert stream.rate == 48000
    heard = listen(engine)
    stream.feed(tone(12000, 1.0, 48000))                 # would alias to 4 kHz
    assert _wait(lambda: heard)
    assert len(heard[0][0]) == RATE and np.sqrt(np.mean(heard[0][0][200:] ** 2)) < 0.005
    assert engine.status()["rate"] == 48000, "the stream's rate, before resampling"


def test_elsewhere_the_microphone_opens_at_16khz_when_it_can(fake_sd, engines, monkeypatch):
    monkeypatch.setattr(A, "_PLUGHW", False)
    fake_sd.boot(FakeDevice(USB, rates=(16000, 44100, 48000), default_rate=48000.0))
    engines()
    assert fake_sd.streams[-1].rate == 16000


def test_resampler_is_seamless_across_blocks():
    from scipy.signal import firwin, lfilter, resample_poly
    rng = np.random.default_rng(1)
    signal = rng.normal(0, 0.2, 48000 * 3).astype(np.float32)
    decimate = A.Resampler(48000, RATE)
    streamed = np.concatenate([decimate(b)[0] for b in np.split(signal, [7, 48000, 100001])])
    taps = firwin(61, 1 / 3, window=("kaiser", 5.0))
    whole = lfilter(taps, 1.0, signal.astype(np.float64))[::3]
    assert len(streamed) == RATE * 3 and np.allclose(streamed, whole, atol=1e-6)

    signal = rng.normal(0, 0.2, 44100 * 3).astype(np.float32)
    poly = A.Resampler(44100, RATE)
    parts = [poly(b) for b in np.split(signal, [1000, 44100, 90000])]
    streamed = np.concatenate([p[0] for p in parts])
    whole = resample_poly(np.concatenate([np.zeros(poly.pad), signal]), 160, 441)
    assert np.allclose(streamed, whole[:len(streamed)], atol=1e-5)
    assert all(0.0 < delay < 0.025 for _, delay in parts)    # ~10 ms of history

    same = A.Resampler(RATE, RATE)
    assert same(signal[:100])[1] == 0.0


def test_resampler_removes_what_16khz_cannot_hold():
    out, _ = A.Resampler(48000, RATE)(tone(12000, 1.0, 48000))   # would alias to 4 kHz
    assert np.sqrt(np.mean(out[200:] ** 2)) < 0.01 * 0.5


# ── hot-plug: when PortAudio lists devices again ──────────────────────────────
def test_device_list_is_not_rebuilt_while_nothing_changes(fake_sd, engines):
    fake_sd.boot()
    engine = engines()
    start = time.monotonic()
    for k in range(1, 100):                       # 50 s of looks, nothing plugged
        engine._watch_once(now=start + k * 0.5)
    assert fake_sd.inits == 0 and fake_sd.terminates == 0
    engine._watch_once(now=start + 61)            # the slow rescan with no microphone
    assert fake_sd.inits == 1
    engine._watch_once(now=start + 62)
    assert fake_sd.inits == 1


def test_device_list_is_not_rebuilt_while_a_microphone_is_open(fake_sd, engines):
    fake_sd.boot(FakeDevice(PI_USB))
    engine = engines()
    stream = fake_sd.streams[-1]
    for _ in range(10):
        stream.feed(seconds=0.1)
        engine._watch_once()
    assert fake_sd.inits == 0 and len(fake_sd.streams) == 1


def test_a_failed_terminate_does_not_skip_initialize(fake_sd, engines):
    fake_sd.boot()
    engine = engines()
    fake_sd.fail_terminate = True
    fake_sd.plugged = [FakeDevice(PI_USB)]
    engine._watch_once()
    assert fake_sd.terminates == 1 and fake_sd.inits == 1
    assert fake_sd.streams[-1].device.name == PI_USB


def test_an_uninitialised_portaudio_is_initialised_without_terminate(fake_sd, engines):
    fake_sd.boot()
    engine = engines()
    fake_sd._initialized = 0                      # an earlier _initialize failed
    fake_sd.plugged = [FakeDevice(PI_USB)]
    engine._watch_once()
    assert fake_sd.terminates == 0 and fake_sd.inits == 1
    assert fake_sd.streams[-1].device.name == PI_USB


def test_a_failing_initialise_warns_once_and_is_not_retried_every_look(fake_sd, engines,
                                                                       records):
    fake_sd.boot()
    engine = engines()
    fake_sd.fail_initialize = True
    fake_sd.plugged = [FakeDevice(PI_USB)]
    start = time.monotonic()
    for k in range(1, 20):
        engine._watch_once(now=start + k * 0.5)
    assert fake_sd.inits == 1 + A._SETTLE_SCANS
    assert sum("failed to initialise" in r.getMessage() for r in records
               if r.levelno == logging.WARNING) == 1


def test_a_microphone_the_sound_server_held_is_found_by_a_settle_scan(fake_sd, engines):
    fake_sd.boot()
    engine = engines()
    usb = FakeDevice(PI_USB)
    usb.busy = usb.hidden = True                  # WirePlumber holds it: PortAudio leaves it out
    fake_sd.plugged = [usb]
    start = time.monotonic()
    engine._watch_once(now=start + 0.5)
    assert fake_sd.inits == 1 and fake_sd.streams == []
    usb.busy = usb.hidden = False                 # released a moment later
    engine._watch_once(now=start + 1.0)
    assert fake_sd.inits == 2 and fake_sd.streams[-1].device is usb


def test_a_better_microphone_that_was_busy_is_switched_to_later(fake_sd, engines):
    builtin, usb = FakeDevice(BUILTIN), FakeDevice(USB)
    usb.busy = True
    fake_sd.boot(builtin, usb, default=BUILTIN)
    engine = engines()
    first = fake_sd.streams[-1]
    assert first.device is builtin
    first.feed()
    assert _wait(lambda: engine.available)
    usb.busy = False
    engine._watch_once()
    assert fake_sd.streams[-1].device is usb and first.closed
    assert fake_sd.inits == 0                     # it was listed already: no rebuild


def test_unplug_and_replug_on_another_card_recovers(fake_sd, engines):
    fake_sd.boot(FakeDevice("USB PnP Sound Device: Audio (hw:2,0)"))
    engine = engines()
    first = fake_sd.streams[-1]
    first.feed()
    assert _wait(lambda: engine.available)
    fake_sd.plugged = []                          # unplugged: the stream stops
    first.active = False
    engine._watch_once()
    assert not engine.available and first.closed and engine.status()["device"] is None
    fake_sd.plugged = [FakeDevice("USB PnP Sound Device: Audio (hw:3,0)")]   # another port
    engine._watch_once()
    second = fake_sd.streams[-1]
    assert second is not first and second.device.name.endswith("(hw:3,0)")
    second.feed()
    assert _wait(lambda: engine.available)
    assert engine.status()["device"] == "USB PnP Sound Device: Audio (hw:3,0)"


def test_a_hanging_close_while_switching_never_reinitialises_under_it(fake_sd, engines,
                                                                      monkeypatch):
    # A better microphone appears (the fingerprint changes) while the open
    # stream's close hangs in its driver: PortAudio must not be terminated
    # until that close has finished (it would free the stream under it).
    monkeypatch.setattr(A, "_CLOSE_TIMEOUT", 0.2)
    monkeypatch.setattr(A, "_LOCK_TIMEOUT", 0.2)
    fake_sd.boot(FakeDevice(BUILTIN, channels=2), default=BUILTIN)
    engine = engines()
    first = fake_sd.streams[-1]
    first.feed()
    assert _wait(lambda: engine.available)
    fake_sd.hang = threading.Event()              # abort() blocks in the driver
    fake_sd.plugged.append(FakeDevice(USB))
    began = time.monotonic()
    engine._watch_once()                          # the close is abandoned after 0.2 s
    assert time.monotonic() - began < 1.5
    assert fake_sd.terminates == 0 and not engine.available
    engine._watch_once()                          # still closing: no rebuild, no terminate
    assert fake_sd.terminates == 0 and len(fake_sd.streams) == 1
    fake_sd.hang.set()                            # the driver lets go
    assert _wait(lambda: not engine._pending_closes())
    engine._watch_once()
    assert first.closed and fake_sd.terminates == 1 and fake_sd.violations == []
    assert fake_sd.streams[-1].device.name == USB


def test_a_hanging_close_does_not_freeze_the_watcher(fake_sd, engines, monkeypatch):
    monkeypatch.setattr(A, "_CLOSE_TIMEOUT", 0.2)
    monkeypatch.setattr(A, "_LOCK_TIMEOUT", 0.2)
    fake_sd.boot(FakeDevice(PI_USB))
    engine = engines()
    stream = fake_sd.streams[-1]
    stream.feed()
    assert _wait(lambda: engine.available)
    fake_sd.hang = threading.Event()              # abort() blocks in the driver
    stream.active = False                         # the device vanished mid-stream
    began = time.monotonic()
    engine._watch_once()
    assert time.monotonic() - began < 1.5
    assert not engine.available and engine.status()["device"] is None
    began = time.monotonic()
    engine._watch_once()                          # PortAudio still held: give up quickly
    assert time.monotonic() - began < 1.5
    assert len(fake_sd.streams) == 1 and fake_sd.inits == 0
    fake_sd.hang.set()                            # the driver lets go
    assert _wait(lambda: not engine._pending_closes())
    engine._watch_once()
    assert len(fake_sd.streams) == 2 and fake_sd.inits == 1


# ── connected means sound is arriving ─────────────────────────────────────────
def test_available_only_once_sound_arrives(fake_sd, engines):
    fake_sd.boot(FakeDevice(PI_USB))
    engine = engines()
    assert fake_sd.streams and engine.available is False
    time.sleep(0.2)
    assert engine.available is False and engine.status()["connected_since"] is None
    fake_sd.streams[-1].feed()
    assert _wait(lambda: engine.available)
    assert engine.microphone == PI_USB


def test_status_reports_the_microphone(fake_sd, engines):
    fake_sd.boot(FakeDevice(PI_USB, rates=(48000,)), FakeDevice(BUILTIN), default=BUILTIN)
    engine = engines()
    assert set(engine.status()) == {
        "available", "device", "index", "hostapi", "rate", "channels", "connected_since",
        "last_error", "blocks", "dropped", "overflows", "last_block_age_s", "candidates"}
    fake_sd.streams[-1].feed(status=SimpleNamespace(input_overflow=True))
    assert _wait(lambda: engine.available)
    status = engine.status()
    assert (status["device"], status["index"], status["hostapi"], status["rate"],
            status["channels"]) == (PI_USB, 0, "MME", 48000, 1)
    assert datetime.fromisoformat(status["connected_since"])
    assert (status["blocks"], status["overflows"], status["dropped"]) == (1, 1, 0)
    assert 0.0 <= status["last_block_age_s"] < 5.0
    assert status["candidates"] == [PI_USB, BUILTIN] and status["last_error"] is None


def test_status_without_a_microphone_and_dropped_blocks_are_counted(fake_sd):
    engine = A.AudioEngine(capture=False)
    status = engine.status()
    assert status["available"] is False and status["device"] is None
    assert status["last_block_age_s"] is None and status["candidates"] == []
    info = SimpleNamespace(inputBufferAdcTime=0.0, currentTime=0.0)
    for _ in range(40):                           # nobody drains the queue
        engine._on_block(np.zeros((RATE, 1), np.float32), RATE, info, None)
    assert (engine.status()["blocks"], engine.status()["dropped"]) == (40, 8)


def test_capture_runs_with_the_sound_model_off(fake_sd):
    fake_sd.boot(FakeDevice(PI_USB))
    engine = A.AudioEngine()                      # model "off" (the fixture), capture on
    try:
        heard = listen(engine)
        assert fake_sd.streams
        fake_sd.streams[-1].feed()
        assert _wait(lambda: heard and engine.available)
        assert engine.result().model == "none"    # nothing classified
    finally:
        engine.stop()


def test_mic_available_is_false_without_a_microphone():
    state = SystemState()
    assert state.get()["mic_available"] is False          # "initializing"
    for label in ("disabled", "mic_unavailable", "audio unavailable",
                  "audio unavailable (stale)", "", None):
        state.update(audio_label=label)
        assert state.get()["mic_available"] is False, label
    for label in ("normal_ambient", "Speech", "scream"):
        state.update(audio_label=label)
        assert state.get()["mic_available"] is True, label
    assert mic_listening("Audio Unavailable") is False


def test_a_microphone_that_records_is_never_reported_off():
    # The sound model off (or no full window yet): nothing is classified,
    # but the microphone captures and recordings get its sound. The dot and
    # the Portal heartbeat follow the engine, as /api/health does.
    state = SystemState()
    state.update(audio_label="audio unavailable", mic_delivering=True)
    data = state.get()
    assert data["mic_available"] is True and data["mic_classifying"] is False
    state.update(audio_label="Speech", mic_delivering=True)
    assert state.get()["mic_classifying"] is True
    state.update(mic_delivering=False)
    assert state.get()["mic_available"] is False, "the engine's word wins once it is known"
    state.update(mic_delivering=None, audio_label="normal_ambient")
    assert state.get()["mic_available"] is True, "before the live loop says: the label"


# ── with the watcher thread, as the app runs it ───────────────────────────────
def test_microphone_plugged_in_after_start_is_picked_up(fake_sd, engines):
    fake_sd.boot()
    engine = engines(watch=True)
    assert engine.available is False              # nothing plugged at start
    fake_sd.plugged = [FakeDevice(PI_USB)]        # the demo: plug the USB mic in
    assert _wait(lambda: fake_sd.streams), "the plugged microphone was never opened"
    fake_sd.streams[-1].feed()
    assert _wait(lambda: engine.available)
    assert engine.microphone == PI_USB
    assert fake_sd.inits >= 1                     # PortAudio listed its devices again


def test_unplugged_microphone_is_reconnected(fake_sd, engines):
    fake_sd.boot(FakeDevice(PI_USB))
    engine = engines(watch=True)
    first = fake_sd.streams[0]
    first.feed()
    assert _wait(lambda: engine.available)
    fake_sd.plugged = []                          # unplug: the stream stops
    first.active = False
    assert _wait(lambda: not engine.available), "the lost microphone was not noticed"
    assert _wait(lambda: first.closed)
    fake_sd.plugged = [FakeDevice(PI_USB)]        # plug it back in
    assert _wait(lambda: len(fake_sd.streams) == 2), "the microphone was not reopened"
    fake_sd.streams[-1].feed()
    assert _wait(lambda: engine.available)


def test_silent_stream_counts_as_unplugged(fake_sd, engines):
    fake_sd.boot(FakeDevice(PI_USB))
    engine = engines(watch=True)
    fake_sd.streams[0].feed()
    assert _wait(lambda: engine.available)
    # the stream stays "active" but no data arrives (a stalled device)
    assert _wait(lambda: not engine.available, timeout=6.0)
    assert _wait(lambda: len(fake_sd.streams) == 2, timeout=6.0)   # and it is retried


def test_stop_ends_the_watcher(fake_sd, engines):
    engine = engines(watch=True)
    engine.stop()
    assert engine._watcher is not None
    assert not engine._watcher.is_alive()
