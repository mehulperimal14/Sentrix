"""Original background music, synthesised here (no samples, no licences): a calm, premium
tech bed in A minor at 96 BPM - warm pads, a soft pulse, a light arpeggio, sub bass - with an
opening swell, an impact under the SENTRIX reveal and a resolved ending.

    music(seconds, out_wav, hits=[t, ...])   # hits: times for a soft impact (scene accents)
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
from scipy.signal import butter, fftconvolve, sosfilt

SR = 48000
BPM = 96
BEAT = 60 / BPM
BAR = 4 * BEAT
RNG = np.random.default_rng(7)

# A minor: Am - F - C - G, then Am - F - Dm - E(sus) for the second half of each 8 bars
PROG = [("A", [57, 60, 64, 69]), ("F", [53, 57, 60, 65]), ("C", [55, 60, 64, 67]), ("G", [55, 59, 62, 67]),
        ("A", [57, 60, 64, 69]), ("F", [53, 57, 60, 65]), ("Dm", [57, 62, 65, 69]), ("Esus", [56, 59, 64, 69])]
ROOT = {"A": 45, "F": 41, "C": 48, "G": 43, "Dm": 50, "Esus": 40}


def hz(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def env(n, a, r, sustain=1.0):
    e = np.full(n, sustain, np.float32)
    na, nr = int(a * SR), int(r * SR)
    e[:na] = np.linspace(0, sustain, na, dtype=np.float32) if na else e[:na]
    if nr:
        e[-nr:] *= np.linspace(1, 0, nr, dtype=np.float32) ** 1.6
    return e


def lowpass(x, f, order=2):
    return sosfilt(butter(order, f / (SR / 2), "low", output="sos"), x, axis=0)


def highpass(x, f, order=2):
    return sosfilt(butter(order, f / (SR / 2), "high", output="sos"), x, axis=0)


def pad_note(m, dur):
    n = int(dur * SR)
    t = np.arange(n) / SR
    out = np.zeros((n, 2), np.float32)
    for k, det in enumerate((-0.11, 0.0, 0.09)):          # three detuned voices, cents/100
        f = hz(m + det)
        ph = RNG.random() * 2 * np.pi
        s = np.zeros(n, np.float32)
        for h in range(1, 14):                               # band-limited saw, softened
            if f * h > 9000:
                break
            s += (np.sin(2 * np.pi * f * h * t + ph * h) / h ** 1.25).astype(np.float32)
        pan = (-0.6, 0.0, 0.6)[k]
        out[:, 0] += s * (1 - pan) * 0.5
        out[:, 1] += s * (1 + pan) * 0.5
    return out


def pluck(m, dur=0.6):
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = hz(m)
    s = (np.sin(2 * np.pi * f * t) * 0.7 + np.sin(2 * np.pi * 2 * f * t) * 0.2 + np.sin(2 * np.pi * 3 * f * t) * 0.08)
    s *= np.exp(-t * 7.5) * env(n, 0.004, 0.05)
    return s.astype(np.float32)


def kick():
    n = int(0.32 * SR)
    t = np.arange(n) / SR
    f = 42 + 70 * np.exp(-t * 28)
    ph = 2 * np.pi * np.cumsum(f) / SR
    k = np.sin(ph) * np.exp(-t * 9)
    k[:144] *= np.linspace(0, 1, 144)
    return k.astype(np.float32)


def hat():
    n = int(0.06 * SR)
    s = RNG.standard_normal(n).astype(np.float32) * np.exp(-np.arange(n) / SR * 70)
    s = sosfilt(butter(2, [6000 / (SR / 2), 11000 / (SR / 2)], "band", output="sos"), s)
    s[:48] *= np.linspace(0, 1, 48)
    return s.astype(np.float32)


def impact():
    n = int(2.6 * SR)
    t = np.arange(n) / SR
    boom = np.sin(2 * np.pi * (34 + 30 * np.exp(-t * 6)) * t) * np.exp(-t * 2.2)
    noise = lowpass(RNG.standard_normal(n), 900) * np.exp(-t * 3.0) * 0.25
    return (boom + noise).astype(np.float32)


def riser(dur):
    n = int(dur * SR)
    t = np.linspace(0, 1, n)
    s = RNG.standard_normal(n) * t ** 2.5
    s = sosfilt(butter(2, [300 / (SR / 2), 6000 / (SR / 2)], "band", output="sos"), s)
    return (s * 0.35).astype(np.float32)


def reverb_ir(seconds=3.2):
    n = int(seconds * SR)
    t = np.arange(n) / SR
    ir = RNG.standard_normal((n, 2)).astype(np.float32) * np.exp(-t * 2.4)[:, None]
    ir = lowpass(ir, 5200)
    ir[: int(0.012 * SR)] = 0
    return (ir / np.sqrt((ir ** 2).sum(axis=0))).astype(np.float32)


def add(buf, x, at, gain=1.0, pan=0.0):
    i = int(at * SR)
    if i >= len(buf):
        return
    x = x[: len(buf) - i]
    if x.ndim == 1:
        buf[i:i + len(x), 0] += x * gain * (1 - pan) * 0.5 * 2 ** 0.5
        buf[i:i + len(x), 1] += x * gain * (1 + pan) * 0.5 * 2 ** 0.5
    else:
        buf[i:i + len(x)] += x * gain


def music(seconds: float, out: Path, hits=()):
    total = seconds + 0.2
    n = int(total * SR)
    pads = np.zeros((n, 2), np.float32)
    arp = np.zeros((n, 2), np.float32)
    bass = np.zeros((n, 2), np.float32)
    drums = np.zeros((n, 2), np.float32)
    fx = np.zeros((n, 2), np.float32)
    end_at = seconds - 4.5                                   # last full chord starts here
    bars = int(np.ceil(end_at / BAR))
    for b in range(bars + 1):
        at = b * BAR
        name, notes = PROG[b % len(PROG)]
        last = at >= end_at - 1e-6 or b == bars
        if last:
            name, notes = "A", [57, 60, 64, 69, 76]
            at = end_at
        dur = (seconds - at + 0.2) if last else BAR + 1.2
        for m in notes:
            add(pads, pad_note(m, dur) * env(int(dur * SR), 0.9, 1.6 if not last else 3.5)[:, None], at, 0.055)
        # sub bass, from bar 2
        if b >= 1:
            r = ROOT[name] if not last else 45
            bt = np.arange(int(dur * SR)) / SR
            sub = np.sin(2 * np.pi * hz(r) * bt) * env(len(bt), 0.05, 0.6 if not last else 3.0)
            add(bass, sub.astype(np.float32), at, 0.16)
        if last:
            break
        # arpeggio from bar 2: chord tones an octave up, 8th notes, gentle velocity shape
        if b >= 2:
            seq = [notes[0] + 12, notes[2] + 12, notes[1] + 12, notes[3] + 12, notes[2] + 12, notes[1] + 12, notes[3] + 12, notes[2] + 12]
            for i, m in enumerate(seq):
                vel = 0.75 + 0.25 * np.sin(i / 7 * np.pi)
                add(arp, pluck(m), at + i * BEAT / 2, 0.06 * vel, pan=(-0.35 if i % 2 else 0.35))
        # pulse from bar 4: kick on 1 and 3, hats on the off-beats
        if b >= 3:
            for k in (0, 2):
                add(drums, kick(), at + k * BEAT, 0.11)
            for k in range(4):
                add(drums, hat(), at + k * BEAT + BEAT / 2, 0.012, pan=0.2)
    # opening swell and impact under the reveal, accents at scene hits
    add(fx, riser(2.2), 0.0, 0.5)
    add(fx, impact(), 2.2, 0.32)
    for h in hits:
        add(fx, impact() * 0.5, h, 0.18)
    # sidechain-style pump on pads and bass from the kick grid (after bar 4)
    tt = np.arange(n) / SR
    pump = 1 - 0.28 * np.exp(-((tt % (2 * BEAT)) / 0.18)) * (tt > 3 * BAR) * (tt < end_at)
    pads *= pump[:, None]
    bass *= pump[:, None]
    dry = pads + arp + bass + drums + fx
    wet = fftconvolve(pads * 0.8 + arp * 1.2 + fx * 0.6, reverb_ir(), axes=0)[:n] * 0.45
    mixd = dry + wet
    mixd = highpass(mixd, 28)
    # master fade in/out
    fade = np.ones(n, np.float32)
    fi, fo = int(0.25 * SR), int(3.5 * SR)
    fade[:fi] = np.linspace(0, 1, fi)
    fade[-fo:] = np.linspace(1, 0, fo) ** 1.5
    mixd *= fade[:, None]
    mixd /= max(1e-6, np.abs(mixd).max()) / 0.89
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(mixd, -1, 1) * 32767).astype(np.int16).tobytes())
    return out


if __name__ == "__main__":
    import sys
    music(float(sys.argv[1]), Path(sys.argv[2]))
