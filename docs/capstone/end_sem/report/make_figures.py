"""Charts for the end-semester report (CPG-299, SENTRIX).

Every number below is copied from a report committed in the repository; the
source is named next to it. Nothing here is measured or estimated anew.

    python docs/capstone/end_sem/report/make_figures.py

writes PDF charts into ``figures/`` next to this file.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

OUT = Path(__file__).resolve().parent / "figures"
OUT.mkdir(exist_ok=True)

plt.rcParams.update({
    "font.family": "serif",
    "font.serif": ["Times New Roman", "Times", "DejaVu Serif"],
    "font.size": 9,
    "axes.titlesize": 9.5,
    "axes.labelsize": 9,
    "legend.fontsize": 8,
    "xtick.labelsize": 8.5,
    "ytick.labelsize": 8.5,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.03,
})

BLUE, ORANGE, GREEN, RED, GREY, PURPLE = "#2563eb", "#ea580c", "#16a34a", "#b91c1c", "#6b7280", "#7c3aed"


def label_bars(ax, bars, fmt="{:.2f}", dy=0.0, size=7.5):
    for b in bars:
        h = b.get_height()
        ax.text(b.get_x() + b.get_width() / 2, h + dy, fmt.format(h), ha="center", va="bottom", fontsize=size)


def save(fig, name):
    fig.savefig(OUT / f"{name}.pdf")
    plt.close(fig)


# 1. Three model configurations on the custom dataset ------------------------
# docs/6_CUSTOM_DATASET_EVALUATION.md §6.7 (held-out val; all-10-video replay),
# CLAUDE.md M1 acceptance (0.79 -> 0.00 false alarms per normal minute).
def configs():
    fig, (a, b) = plt.subplots(1, 2, figsize=(6.3, 2.5))
    names = ["Baseline\n(v3)", "Fine-tuned\n(v3)", "Custom\n(v4)"]
    acc = [0.417, 0.583, 0.679]
    f1 = [0.281, 0.401, 0.768]
    x = range(3)
    b1 = a.bar([i - 0.19 for i in x], acc, 0.38, color=BLUE, label="Accuracy")
    b2 = a.bar([i + 0.19 for i in x], f1, 0.38, color=ORANGE, label="Macro-F1")
    label_bars(a, b1, dy=0.01)
    label_bars(a, b2, dy=0.01)
    a.set_xticks(list(x), names)
    a.set_ylim(0, 1)
    a.set_title("(a) Validation split, per image")
    a.legend(loc="upper left", frameon=False)

    names2 = ["Baseline\n(v3)", "Fine-tuned\n(v3)", "Custom\n(v4)", "Custom (v4)\n+ M1 rules"]
    fa = [13.96, 7.12, 0.79, 0.00]
    bars = b.bar(range(4), fa, 0.6, color=[GREY, GREY, BLUE, GREEN])
    label_bars(b, bars, dy=0.2)
    b.set_xticks(range(4), names2)
    b.set_ylabel("False alarms per normal minute")
    b.set_ylim(0, 16)
    b.set_title("(b) False alarms, replay of all 10 videos")
    fig.tight_layout()
    save(fig, "fig_configs")


# 2. The first live session re-decided ---------------------------------------
# CLAUDE.md §5 and M1 (30/9/6 -> 0/0/0, home mode), §4.3 M11b row (home 5/0/0,
# armed 9/0/0; the 5 / 9 are the by-design unrecognised-person L2s).
def live_session():
    fig, ax = plt.subplots(figsize=(6.0, 2.4))
    groups = ["L2 and above", "L3 and above", "L4 and above"]
    before = [30, 9, 6]
    m1 = [0, 0, 0]
    final = [5, 0, 0]
    x = range(3)
    w = 0.26
    b1 = ax.bar([i - w for i in x], before, w, color=RED, label="As logged live (v3 pipeline)")
    b2 = ax.bar(list(x), m1, w, color=BLUE, label="After M1 containment")
    b3 = ax.bar([i + w for i in x], final, w, color=GREEN, label="Final rules (D15: unknown person = L2 only)")
    for bars in (b1, b2, b3):
        label_bars(ax, bars, fmt="{:.0f}", dy=0.4)
    ax.set_xticks(list(x), groups)
    ax.set_ylabel("Alarm onsets in 18.3 min")
    ax.set_ylim(0, 34)
    ax.legend(frameon=False, loc="upper right")
    fig.tight_layout()
    save(fig, "fig_live_session")


# 3. Episodes caught at each committed level ---------------------------------
# CLAUDE.md §4.3, M11b tick replay with soundtracks (= M5 at L3+):
# weapon L3 9/12, L4 6/12, L5 2/12; fire 3/3, 2/3, 2/3.
def levels():
    fig, ax = plt.subplots(figsize=(5.6, 2.4))
    lv = ["Level 3\n(snapshot, evidence, SOS)", "Level 4\n(dispatch package)", "Level 5\n(critical)"]
    weapon = [9, 6, 2]
    fire = [3, 2, 2]
    x = range(3)
    w = 0.36
    bw = ax.bar([i - w / 2 for i in x], [v / 12 * 100 for v in weapon], w, color=BLUE, label="Handgun (12 episodes)")
    bf = ax.bar([i + w / 2 for i in x], [v / 3 * 100 for v in fire], w, color=ORANGE, label="Flame (3 episodes)")
    for bars, vals, n in ((bw, weapon, 12), (bf, fire, 3)):
        for bar, v in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1.5, f"{v}/{n}", ha="center", fontsize=7.5)
    ax.set_xticks(list(x), lv)
    ax.set_ylabel("Episodes reached (%)")
    ax.set_ylim(0, 115)
    ax.legend(frameon=False, loc="upper right")
    fig.tight_layout()
    save(fig, "fig_levels")


# 4. Live loop before and after M2 -------------------------------------------
# CLAUDE.md §5 (3086 ms per tick = 0.32 Hz live config; HUD 0.6 FPS) and M2
# acceptance (tools/bench_live.py, vid07 with four faces: preview 29.7 fps,
# AI 5.6 Hz, tick p50 168 ms; per-stage identity 125, scene 94, YOLO 25/23/22).
def live_loop():
    fig, (a, b) = plt.subplots(1, 2, figsize=(6.3, 2.4), gridspec_kw={"width_ratios": [1.1, 1]})
    cats = ["Preview video\n(fps)", "AI decisions\n(Hz)"]
    before = [0.6, 0.32]
    after = [29.7, 5.6]
    x = range(2)
    b1 = a.bar([i - 0.19 for i in x], before, 0.38, color=RED, label="First live run")
    b2 = a.bar([i + 0.19 for i in x], after, 0.38, color=GREEN, label="After M2")
    label_bars(a, b1, fmt="{:.2f}", dy=0.4)
    label_bars(a, b2, fmt="{:.1f}", dy=0.4)
    a.set_xticks(list(x), cats)
    a.set_ylim(0, 35)
    a.set_title("(a) Rates on the laptop CPU")
    a.legend(frameon=False, loc="upper right")

    stages = ["Identity\n(own thread)", "Scene\n(DINOv2)", "YOLO\nperson", "YOLO\nweapon", "YOLO\nfire"]
    ms = [125, 94, 25, 23, 22]
    bars = b.bar(range(5), ms, 0.6, color=[PURPLE, BLUE, GREY, GREY, GREY])
    label_bars(b, bars, fmt="{:.0f}", dy=2)
    b.set_xticks(range(5), stages)
    b.set_ylabel("ms per call (p50)")
    b.set_ylim(0, 145)
    b.set_title("(b) Stage cost, live bench")
    fig.tight_layout()
    save(fig, "fig_live_loop")


# 5. Clip engine on the Raspberry Pi 5 ---------------------------------------
# training/runs/custom_eval/pi5/REPORT.md (bench_clip on the Pi, gun.mov).
def pi_clip():
    fig, (a, b) = plt.subplots(1, 2, figsize=(6.3, 2.5))
    names = ["640 px,\n2 scene frames", "320 px,\n2 scene frames\n(shipped)", "320 px,\n1 scene frame"]
    p50 = [7.983, 3.853, 2.177]
    p95 = [8.071, 3.940, 2.712]
    x = range(3)
    b1 = a.bar([i - 0.19 for i in x], p50, 0.38, color=BLUE, label="p50")
    b2 = a.bar([i + 0.19 for i in x], p95, 0.38, color=ORANGE, label="p95")
    label_bars(a, b1, fmt="{:.1f}", dy=0.1)
    label_bars(a, b2, fmt="{:.1f}", dy=0.1)
    a.axhline(1.6, color=RED, ls="--", lw=1, label="1.6 s budget")
    a.set_xticks(list(x), names)
    a.set_ylabel("Clip compute (s)")
    a.set_ylim(0, 9.5)
    a.set_title("(a) Time to analyse one 4 s clip")
    a.legend(frameon=False, loc="upper right")

    dropped = [74.2, 48.4, 10.0]
    age = [9.3, 4.3, 3.2]
    bars = b.bar([i - 0.19 for i in x], dropped, 0.38, color=GREY, label="Clips dropped (%)")
    label_bars(b, bars, fmt="{:.0f}", dy=1)
    b2 = b.twinx()
    b2.spines["top"].set_visible(False)
    bb = b2.bar([i + 0.19 for i in x], age, 0.38, color=GREEN, label="Decision age p50 (s)")
    label_bars(b2, bb, fmt="{:.1f}", dy=0.1)
    b.set_ylabel("Clips dropped (%)")
    b2.set_ylabel("Decision age (s)")
    b.set_ylim(0, 85)
    b2.set_ylim(0, 11)
    b.set_xticks(list(x), names)
    b.set_title("(b) Coverage and freshness")
    h1, l1 = b.get_legend_handles_labels()
    h2, l2 = b2.get_legend_handles_labels()
    b.legend(h1 + h2, l1 + l2, frameon=False, loc="upper right", fontsize=7)
    fig.tight_layout()
    save(fig, "fig_pi_clip")


# 6. Slim scene heads: accuracy vs speed --------------------------------------
# training/runs/custom/slim/REPORT.md (LOVO macro-F1; 4 threads, ONNX Runtime).
def slim():
    fig, ax = plt.subplots(figsize=(4.6, 2.6))
    pts = [("DINOv2 ViT-B/14 (shipped)", 106.4, 0.440, BLUE),
           ("MobileNetV3-L", 2.4, 0.229, ORANGE),
           ("EfficientNet-B0 (distilled)", 4.3, 0.211, PURPLE)]
    for name, ms, f1, c in pts:
        ax.scatter([ms], [f1], s=45, color=c, zorder=3)
        ax.annotate(name, (ms, f1), textcoords="offset points", xytext=(6, 4), fontsize=7.5)
    ax.axhline(0.396, color=RED, ls="--", lw=1)
    ax.text(1.6, 0.405, "pre-registered bar 0.396", color=RED, fontsize=7.5)
    ax.set_xscale("log")
    ax.set_xlim(1.5, 400)
    ax.set_ylim(0.15, 0.5)
    ax.set_xlabel("ms per frame (ONNX Runtime, 4 threads, log scale)")
    ax.set_ylabel("Leave-one-video-out macro-F1")
    fig.tight_layout()
    save(fig, "fig_slim")


# 7. Body re-identification ---------------------------------------------------
# (a) pairs on the nine train + val videos: training/runs/reid/osnet_x1_0_imagenet/REPORT.md
#     (histogram 0.7283, ImageNet 0.8306) and osnet_weights_compare/REPORT.md
#     (x1_0 MSMT17 0.9172, AIN MSMT17 0.931).
# (b) identity rule at each model's bar, nine videos: osnet_weights_compare/REPORT.md
#     (ImageNet 0.85: 37.3 % / 0.038 %; x1_0 0.84: 39.0 / 0.030; AIN 0.86: 42.3 / 0.042);
#     shipped, fourteen videos: osnet_ain_14videos/REPORT.md (AIN 0.87: 30.1 / 0.020).
def reid():
    fig, (a, b) = plt.subplots(1, 2, figsize=(6.4, 2.6))
    names = ["Colour\nhistogram", "OSNet\nImageNet", "OSNet\nMSMT17", "OSNet-AIN\nMSMT17"]
    au = [0.7283, 0.8306, 0.9172, 0.931]
    bars = a.bar(range(4), au, 0.6, color=[GREY, GREY, BLUE, GREEN])
    label_bars(a, bars, fmt="{:.2f}", dy=0.01)
    a.set_xticks(range(4), names, fontsize=7)
    a.set_ylim(0.5, 1.02)
    a.set_ylabel("AUROC (pairs)")
    a.set_title("(a) Same vs different person")

    labels = ["ImageNet\n0.85", "MSMT17\n0.84", "AIN\n0.86", "AIN 0.87\n(shipped,\n14 videos)"]
    same = [37.3, 39.0, 42.3, 30.1]
    diff = [0.038, 0.030, 0.042, 0.020]
    x = range(4)
    s1 = b.bar(list(x), same, 0.6, color=[GREY, BLUE, GREEN, GREEN])
    label_bars(b, s1, fmt="{:.0f}%", dy=1)
    for i, d in zip(x, diff):
        b.text(i, 3, f"diff.\n{d:.3f}%", ha="center", va="bottom", fontsize=6.5, color="white")
    b.set_xticks(list(x), labels, fontsize=6.8)
    b.set_ylim(0, 55)
    b.set_ylabel("Same person accepted (%)")
    b.set_title("(b) Identity rule at its bar")
    fig.tight_layout()
    save(fig, "fig_reid")


# 8. Scene scores in the demo room (Raspberry Pi 5, no threat in view) ---------
# training/runs/calibration/pi5_demo_room_2026-11-19/calibrate_room.json: the whole
# log 2026-10-23 .. 11-19 (9,092 scene-scored clips, 5.08 h): weapon median 0.1896,
# p99 0.5464, p99.9 0.6668, max 0.7602; fire 0.0792, 0.2361, 0.5685, 0.7164.
# Floors 0.50 / 0.45; hand thresholds 0.75 / 0.70; calibrated, in force since
# 2026-11-19: 0.78 / 0.75.
def room():
    fig, ax = plt.subplots(figsize=(6.0, 2.6))
    q = ["Median", "p99", "p99.9", "Max"]
    weapon = [0.1896, 0.5464, 0.6668, 0.7602]
    fire = [0.0792, 0.2361, 0.5685, 0.7164]
    x = range(4)
    bw = ax.bar([i - 0.19 for i in x], weapon, 0.38, color=BLUE, label="p(weapon)")
    bf = ax.bar([i + 0.19 for i in x], fire, 0.38, color=ORANGE, label="p(fire)")
    label_bars(ax, bw, fmt="{:.2f}", dy=0.01)
    label_bars(ax, bf, fmt="{:.2f}", dy=0.01)
    # (y, colour, label, x, label offset): the two room lines are 0.03 apart, so
    # one label sits above its line and the other below, clear of the bars.
    for y, c, txt, lx, dy in ((0.50, BLUE, "weapon floor 0.50", -0.42, 0.008),
                              (0.45, ORANGE, "fire floor 0.45", -0.42, -0.04),
                              (0.78, BLUE, "weapon, this room 0.78", 0.42, 0.008),
                              (0.75, ORANGE, "fire, this room 0.75", 0.42, -0.04)):
        ax.axhline(y, color=c, ls=":" if "floor" in txt else "--", lw=0.9)
        ax.text(lx, y + dy, txt, color=c, fontsize=6.8, ha="left")
    ax.set_xticks(list(x), q)
    ax.set_ylim(0, 0.9)
    ax.set_ylabel("Scene-model probability")
    ax.legend(frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=2)
    fig.tight_layout()
    save(fig, "fig_room_scores")


# 9. Work schedule (from the git history and CLAUDE.md §2) ---------------------
def gantt():
    import datetime as dt

    D = dt.date.fromisoformat
    rows = [
        ("Prototype v3 and mid-sem report", "2026-08-21", "2026-08-25", GREY),
        ("Custom dataset, evaluation harness, v4 models", "2026-08-26", "2026-09-30", BLUE),
        ("First live run and diagnosis", "2026-09-30", "2026-10-01", RED),
        ("End-to-end repair M1-M5", "2026-10-01", "2026-10-10", BLUE),
        ("Portal, SOS, link, Pi I/O, clip engine (8 tracks)", "2026-10-10", "2026-10-15", PURPLE),
        ("Multimodal fusion, sound model (M11)", "2026-10-15", "2026-10-16", PURPLE),
        ("Pi 5 bring-up, Render Portal, Telegram", "2026-10-23", "2026-10-25", GREEN),
        ("Enclosure, recordings, microphone, bug sweep", "2026-10-24", "2026-10-28", GREEN),
        ("React frontend v2", "2026-10-28", "2026-11-18", ORANGE),
        ("UX clean-up, wiring audit", "2026-11-01", "2026-11-02", ORANGE),
        ("Body re-identification (OSNet)", "2026-11-18", "2026-11-19", BLUE),
        ("New videos, v5 detector heads, room calibration", "2026-11-18", "2026-11-20", GREEN),
        ("Report, poster, video, evaluations", "2026-11-18", "2026-12-05", GREY),
    ]
    fig, ax = plt.subplots(figsize=(6.3, 3.2))
    for i, (name, s, e, c) in enumerate(rows):
        start, end = D(s), D(e)
        ax.barh(i, max((end - start).days, 1), left=matplotlib.dates.date2num(start), color=c, height=0.6)
    ax.set_yticks(range(len(rows)), [r[0] for r in rows], fontsize=7.5)
    ax.invert_yaxis()
    ax.xaxis_date()
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%d %b"))
    ax.set_xlim(matplotlib.dates.date2num(D("2026-08-15")), matplotlib.dates.date2num(D("2026-12-08")))
    for d, lab in (("2026-11-30", "Evaluation"), ("2026-12-05", "Final panel")):
        ax.axvline(matplotlib.dates.date2num(D(d)), color=RED, lw=0.8, ls="--")
    ax.grid(axis="x", lw=0.3, alpha=0.6)
    fig.tight_layout()
    save(fig, "fig_gantt")


# 10. Automated tests per area -----------------------------------------------
# pytest --collect-only on branch sentrix/portal (3e1e296): 2128 tests in 71
# files, grouped by the file-name prefix of each test module.
def tests():
    areas = [
        ("Perception and fusion", 276), ("Channels and send guard", 244), ("SOS engine and machine", 238),
        ("Recording and microphone", 223), ("Pi hardware and deploy kit", 213), ("Contracts and integration", 168),
        ("Link, tunnel, path table", 160), ("Local Console and web security", 159), ("Clip engine and fast lane", 148),
        ("Portal accounts and onboarding", 144), ("Evaluation, deploy, tools", 118), ("Pipeline, records, live loop", 63),
    ]
    fig, ax = plt.subplots(figsize=(6.0, 3.0))
    names = [a for a, _ in areas][::-1]
    vals = [v for _, v in areas][::-1]
    bars = ax.barh(range(len(vals)), vals, color=BLUE, height=0.65)
    for b, v in zip(bars, vals):
        ax.text(v + 3, b.get_y() + b.get_height() / 2, str(v), va="center", fontsize=7.5)
    ax.set_yticks(range(len(vals)), names, fontsize=8)
    ax.set_xlabel("Automated tests (2154 in total)")
    ax.set_xlim(0, 310)
    fig.tight_layout()
    save(fig, "fig_tests")


if __name__ == "__main__":
    for f in (configs, live_session, levels, live_loop, pi_clip, slim, reid, room, gantt, tests):
        f()
        print("wrote", f.__name__)
