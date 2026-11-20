"""Build the SENTRIX submission videos (one per student) with narration, Kartik Garg's own voice for the team
section in his video, an original music bed, and captions.

    python build.py voice        # narration WAVs + Kartik's cleaned lines, prints the timeline
    python build.py render [ids] # scenes -> silent MP4s (all, or the given scene ids / 'title')
    python build.py assemble     # pieces, audio mix, final MP4s, SRT, NARRATION.md
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
from look import page  # noqa: E402
from scenes import BUILDERS, CROPS, VID, caption_times, title  # noqa: E402
from story import ROLES, SCENES, STUDENTS, title_speech_team  # noqa: E402

HERE = Path(__file__).resolve().parent
VO, SCN, SEG, OUT = HERE / "vo", HERE / "scenes", HERE / "seg", HERE / "out"
for d in (VO, SCN, SEG, OUT):
    d.mkdir(exist_ok=True)
KOKORO = HERE.parent / "kokoro"
VOICE, SPEED = "am_michael", 1.1
KARTIK = HERE.parent / "kvoice" / "raw48.wav"
KARTIK_LINES = [(0.91, 9.74), (11.02, 21.44), (22.23, 30.48), (31.48, 40.13), (41.55, 54.43)]   # measured pauses
SR = 48000
FPS = 30
X = 0.5                      # crossfade between scenes inside a piece
LEAD, GAP, TAIL = 0.85, 0.38, 1.15
TITLE_MIN = 13.0


def sh(*a):
    subprocess.run([str(x) for x in a], check=True)


def ff(*a):
    sh("ffmpeg", "-y", "-loglevel", "error", *a)


# ── voice ──────────────────────────────────────────────────────────────────────────────────
def vpath(text):
    return VO / (hashlib.sha1(f"{VOICE}|{SPEED}|{text}".encode()).hexdigest()[:16] + ".wav")


def synth(texts):
    todo = [t for t in dict.fromkeys(texts) if not vpath(t).exists()]
    if todo:
        jobs = VO / "jobs.tsv"
        jobs.write_text("".join(f"{vpath(t)}\t{t}\n" for t in todo), encoding="utf-8")
        sh(KOKORO / "venv" / "Scripts" / "python.exe", "-I", HERE / "tools" / "kokoro_tts.py", jobs, KOKORO / "model", VOICE, SPEED)


def read_wav(p) -> tuple[np.ndarray, int]:
    with wave.open(str(p)) as w:
        sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        a = np.frombuffer(w.readframes(n), np.int16).astype(np.float32) / 32768
    if ch > 1:
        a = a.reshape(-1, ch).mean(1)
    return a, sr


def load_voice(text) -> np.ndarray:
    a, sr = read_wav(vpath(text))
    loud = np.flatnonzero(np.abs(a) > 0.02 * max(1e-6, np.abs(a).max()))
    if loud.size:
        m = int(0.03 * sr)
        a = a[max(0, loud[0] - m): loud[-1] + m]
    from scipy.signal import resample_poly
    return resample_poly(a, 2, 1).astype(np.float32) if sr == 24000 else a


def kartik_lines() -> list[np.ndarray]:
    """Kartik's recording, cleaned (rumble cut, light de-noise, de-mud, presence, gentle compression), one array per line."""
    clean = VO / "kartik_clean.wav"
    if not clean.exists():
        ff("-i", KARTIK, "-af",
           "highpass=f=75,afftdn=nf=-48:tn=1,equalizer=f=230:t=q:w=1.0:g=-2.5,equalizer=f=3600:t=q:w=1.3:g=2.5,"
           "treble=g=1.5:f=9000,acompressor=threshold=-21dB:ratio=2.4:attack=8:release=170:makeup=1.5,alimiter=limit=0.93",
           "-ac", 1, "-ar", SR, clean)
    a, sr = read_wav(clean)
    out = []
    for s, e in KARTIK_LINES:
        seg = a[int((s - 0.06) * sr): int((e + 0.12) * sr)].copy()
        f = int(0.015 * sr)
        seg[:f] *= np.linspace(0, 1, f)
        seg[-f:] *= np.linspace(1, 0, f)
        out.append(seg)
    return out


def rms(a):
    v = a[np.abs(a) > 0.02]
    return float(np.sqrt(np.mean(v ** 2))) if v.size else 0.1


def spoken(line):
    return line[1] or line[0]


# ── timeline ───────────────────────────────────────────────────────────────────────────────
def timeline():
    """Scene durations and sentence times (scene-relative). The team scene's slots fit both voices."""
    kl = kartik_lines()
    plan = []
    for sid, kind, chap, lines, o in SCENES:
        if sid == "team":
            lens = [max(len(kl[k]) / SR, len(load_voice(spoken(lines[k]))) / SR) for k in range(len(lines))]
        else:
            lens = [len(load_voice(spoken(l))) / SR for l in lines]
        T, t = [], LEAD
        for L in lens:
            T.append((round(t, 3), round(t + L, 3)))
            t += L + GAP
        dur = round(max(o.get("min", 0.0), T[-1][1] + TAIL), 2)
        if sid == "close":
            dur = round(dur + 2.0, 2)
        plan.append({"id": sid, "kind": kind, "chapter": chap, "lines": lines, "o": o, "T": T, "dur": dur})
    tdur = max(TITLE_MIN, len(load_voice(title_speech_team())) / SR + 1.6)
    return plan, round(tdur, 2)


PIECES = {"pre": ["problem", "hero"], "team": ["team"], "post": None}   # post = everything after the team scene


def piece_scenes(plan, name):
    ids = [p["id"] for p in plan]
    if name == "post":
        return plan[ids.index("team") + 1:]
    return [p for p in plan if p["id"] in PIECES[name]]


# ── render ─────────────────────────────────────────────────────────────────────────────────
def mask(w, h, r):
    p = SEG / f"mask_{w}x{h}_{r}.png"
    if not p.exists():
        im = Image.new("L", (w * 4, h * 4), 0)
        ImageDraw.Draw(im).rounded_rectangle((0, 0, w * 4 - 1, h * 4 - 1), r * 4, fill=255)
        im.resize((w, h), Image.LANCZOS).save(p)
    return p


def composite(src, overlays, dur, out):
    """Put the app clips into their slots: crop the page, scale to the slot, round the corners, fade in."""
    if not overlays:
        src.replace(out)
        return
    ov = overlays[0]
    cx, cy, cw, chh = CROPS[ov["clip"]]
    x, y, w, h = ov["rect"]
    clip = Path(VID) / "clips" / f"{ov['clip']}.mp4"
    m = mask(w, h, ov["radius"]) if ov["radius"] else None
    t_in = ov["t_in"]
    chain = (f"[1:v]setpts=PTS-STARTPTS,crop={cw}:{chh}:{cx}:{cy},scale={w}:{h}:flags=lanczos,"
             f"tpad=stop_mode=clone:stop_duration={dur:.2f},trim=0:{dur:.3f},setpts=PTS-STARTPTS+{t_in}/TB,format=yuva420p")
    if m:
        chain += f"[c];[2:v]format=gray,scale={w}:{h}[m];[c][m]alphamerge"
    chain += f",fade=t=in:st={t_in}:d=0.35:alpha=1[ov];[0:v][ov]overlay={x}:{y}:eof_action=pass:format=auto,format=yuv420p[v]"
    args = ["-i", src, "-ss", ov["ss"], "-i", clip] + (["-loop", 1, "-i", m] if m else [])
    ff(*args, "-filter_complex", chain, "-map", "[v]", "-t", f"{dur:.3f}", "-r", FPS,
       "-c:v", "libx264", "-preset", "medium", "-crf", 14, "-pix_fmt", "yuv420p", out)


def scene_html(p, voice_chip=""):
    o = dict(p["o"])
    if p["id"] == "team":
        o["voice_chip"] = voice_chip
    body, overlays = BUILDERS[p["kind"]](p["T"], p["dur"], p["lines"], o)
    return page(body), overlays


def render(only=None):
    plan, tdur = timeline()
    jobs, comps = [], []
    for p in plan:
        variants = [("team_kartik", "In Kartik Garg's own voice")] if p["id"] == "team" else [(p["id"], "")]
        for name, chip in variants:
            if only and name not in only and p["id"] not in only:
                continue
            html, overlays = scene_html(p, chip)
            hp = SCN / f"{name}.html"
            hp.write_text(html, encoding="utf-8")
            raw = SEG / f"{name}.raw.mp4"
            jobs.append({"html": str(hp), "dur": p["dur"], "out": str(raw)})
            comps.append((raw, overlays, p["dur"], SEG / f"{name}.mp4"))
    if not only or "title" in only:
        body, _ = title(STUDENTS, tdur)
        hp = SCN / "title_team.html"
        hp.write_text(page(body), encoding="utf-8")
        jobs.append({"html": str(hp), "dur": tdur, "out": str(SEG / "title_team.mp4")})
    jf = SEG / "jobs.json"
    jf.write_text(json.dumps(jobs), encoding="utf-8")
    sh("node", HERE / "render.mjs", jf, 4)
    for raw, overlays, dur, out in comps:
        composite(raw, overlays, dur, out)
        print("composited", out.name)


# ── assemble ───────────────────────────────────────────────────────────────────────────────
ENC = ["-c:v", "libx264", "-preset", "slow", "-crf", 17, "-pix_fmt", "yuv420p", "-r", FPS, "-g", 60, "-bf", 2,
       "-profile:v", "high", "-level", "4.1", "-movflags", "+faststart"]


def piece(name, files, durs, out):
    """xfade the scenes of one piece, fade in from and out to black at its ends."""
    total = sum(durs) - X * (len(durs) - 1)
    if len(files) == 1:
        fc = f"[0:v]fade=t=in:st=0:d=0.4,fade=t=out:st={total - 0.4:.3f}:d=0.4,format=yuv420p[v]"
    else:
        parts, prev, off = [], "[0:v]", 0.0
        for k in range(1, len(files)):
            off += durs[k - 1] - X
            lab = f"[x{k}]"
            parts.append(f"{prev}[{k}:v]xfade=transition=fade:duration={X}:offset={off:.3f}{lab}")
            prev = lab
        fc = ";".join(parts) + f";{prev}fade=t=in:st=0:d=0.4,fade=t=out:st={total - 0.4:.3f}:d=0.4,format=yuv420p[v]"
    args = []
    for f in files:
        args += ["-i", f]
    ff(*args, "-filter_complex", fc, "-map", "[v]", "-t", f"{total:.3f}", *ENC, out)
    return total


def assemble():
    import music as mus
    plan, tdur = timeline()
    kl = kartik_lines()
    # pieces (shared), and their scene offsets
    offs = {}
    pieces = {}
    for name in ("pre", "team", "post"):
        ps = piece_scenes(plan, name)
        t = 0.0
        for p in ps:
            offs[p["id"]] = t
            t += p["dur"] - X
        pieces[name] = ps
    lens = {}
    for name, ps in pieces.items():
        files = [SEG / (f"{p['id']}_kartik.mp4" if p["id"] == "team" else f"{p['id']}.mp4") for p in ps]
        lens[name] = piece(name, files, [p["dur"] for p in ps], SEG / f"piece_{name}.mp4")
    ff("-i", SEG / "title_team.mp4", "-t", f"{tdur:.3f}", *ENC, SEG / "piece_title_team.mp4")

    base = {"pre": tdur, "team": tdur + lens["pre"], "post": tdur + lens["pre"] + lens["team"]}
    total = base["post"] + lens["post"]
    which = {p["id"]: name for name, ps in pieces.items() for p in ps}

    def absolute(sid):
        return base[which[sid]] + offs[sid]

    # music: accents at the chapter starts and the close
    hits = [absolute(s) + 0.2 for s in ("programs", "physical", "inputs", "start", "run", "results1", "close")]
    mw = SEG / "music.wav"
    mus.music(total, mw, hits=hits)
    with wave.open(str(mw)) as w:
        msr = w.getframerate()
        mraw = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32).reshape(-1, 2) / 32768
    from scipy.signal import resample_poly
    music_arr = resample_poly(mraw, SR, msr, axis=0).astype(np.float32) if msr != SR else mraw

    narr_rms = np.median([rms(load_voice(spoken(l))) for p in plan for l in p["lines"]])
    N = int((total + 1) * SR)
    vo = np.zeros(N, np.float32)

    def put(a, t):
        i = int(t * SR)
        vo[i:i + len(a)] += a[: max(0, N - i)]

    put(load_voice(title_speech_team()), 0.7)
    rows = []
    for p in plan:
        t0 = absolute(p["id"])
        for k, ((a, b), line) in enumerate(zip(p["T"], p["lines"])):
            if p["id"] == "team":                       # the roles in Kartik Garg's own voice
                put(kl[k] * (narr_rms / rms(kl[k])), t0 + a)
            else:
                put(load_voice(spoken(line)), t0 + a)
        if p["o"].get("captions", True) is not False:
            rows += [(t0 + a, t0 + b, c) for a, b, c in caption_times(p["lines"], p["T"])]
        else:
            rows += [(t0 + a, t0 + b, line[0]) for (a, b), line in zip(p["T"], p["lines"])]
    # duck the music under the voice: fast attack, slow release
    hop = int(0.01 * SR)
    env = np.sqrt(np.convolve(vo ** 2, np.ones(hop) / hop, mode="same"))
    sp = np.clip(env / (narr_rms * 0.5), 0, 1)
    sm = np.zeros_like(sp)
    acc, step = 0.0, 48
    att, rel = np.exp(-step / (0.05 * SR)), np.exp(-step / (0.55 * SR))
    for i in range(0, N, step):
        x = sp[i]
        acc = (att if x > acc else rel) * (acc - x) + x
        sm[i:i + step] = acc
    m = np.zeros((N, 2), np.float32)
    L = min(N, len(music_arr))
    m[:L] = music_arr[:L]
    base_gain = narr_rms / (np.sqrt(np.mean(m ** 2)) + 1e-6) * 10 ** (-15 / 20)
    mix = m * (base_gain * (1 - 0.6 * sm))[:, None] + np.stack([vo, vo], 1)
    mix /= max(1.0, np.abs(mix).max() / 0.97)
    mixw = SEG / "mix_team.wav"
    with wave.open(str(mixw), "wb") as w:
        w.setnchannels(2), w.setsampwidth(2), w.setframerate(SR)
        w.writeframes((np.clip(mix, -1, 1) * 32767).astype(np.int16).tobytes())
    lst = SEG / "list_team.txt"
    lst.write_text("".join(f"file '{(SEG / f).as_posix()}'\n" for f in
                           ("piece_title_team.mp4", "piece_pre.mp4", "piece_team.mp4", "piece_post.mp4")), encoding="utf-8")
    silent = SEG / "silent_team.mp4"
    ff("-f", "concat", "-safe", 0, "-i", lst, "-c", "copy", silent)
    out = OUT / "SENTRIX_CPG299_Video.mp4"
    ff("-i", silent, "-i", mixw, "-map", "0:v", "-map", "1:a", "-c:v", "copy",
       "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", 48000, "-c:a", "aac", "-b:a", "192k", "-shortest",
       "-movflags", "+faststart", out)
    print("->", out, f"{total:.1f} s")

    def ts(t):
        h, rem = divmod(t, 3600)
        mi, s = divmod(rem, 60)
        return f"{int(h):02d}:{int(mi):02d}:{int(s):02d},{int(round((s - int(s)) * 1000)):03d}"
    (OUT / "narration.srt").write_text("\n".join(f"{k}\n{ts(a)} --> {ts(b)}\n{c}\n" for k, (a, b, c) in enumerate(rows, 1)),
                                       encoding="utf-8")
    md = ["# SENTRIX submission video: narration", "",
          f"One video for the team, {int(total // 60)} min {int(total % 60)} s. 0:00-0:{int(tdur):02d} is the title card "
          f"(the five students and their roll numbers, the project, its title, the university), shown from the first "
          f"frame. The narrator is Kokoro's 'Michael' voice; the team's roles are in Kartik Garg's own voice.", "",
          f"**0:00 · Title** — {title_speech_team()}", ""]
    for p in plan:
        t0 = absolute(p["id"])
        md.append(f"**{int(t0 // 60)}:{int(t0 % 60):02d} · {p['id']}**  ")
        md.append(" ".join(l[0] for l in p["lines"]))
        md.append("")
    (OUT / "NARRATION.md").write_text("\n".join(md), encoding="utf-8")

def show():
    plan, tdur = timeline()
    t = tdur
    print(f"title {tdur:.1f}s")
    for p in plan:
        print(f"{t:7.1f}  {p['id']:10} {p['dur']:5.1f}s  ch{p['chapter']}  " + " | ".join(f"{a:.1f}-{b:.1f}" for a, b in p["T"]))
        t += p["dur"] - X
    print(f"total ~{t:.0f} s")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "voice"
    if cmd == "voice":
        texts = [spoken(l) for _, _, _, lines, _ in SCENES for l in lines] + [title_speech_team()]
        synth(texts)
        show()
    elif cmd == "render":
        render(set(sys.argv[2:]) or None)
    elif cmd == "assemble":
        assemble()
