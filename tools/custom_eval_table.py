"""Markdown comparison tables from ``training/runs/custom_eval/*``.

Reads the ``frames.json`` and ``replay_all.json`` reports that
``tools/eval_custom_frames.py`` and ``tools/replay_custom_videos.py`` wrote,
so documented numbers are generated, never transcribed.

Usage::

    python tools/custom_eval_table.py                      # every run found
    python tools/custom_eval_table.py baseline finetuned   # chosen runs, in order
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

RUNS = Path(__file__).resolve().parent.parent / "training" / "runs" / "custom_eval"


def _load(tag: str, name: str) -> dict | None:
    path = RUNS / tag / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _f(x) -> str:
    return "-" if x is None else f"{x:.3f}"


def frame_table(tags: list[str]) -> str:
    lines = ["| Run | Split | n | Accuracy | Macro-F1 | Threat recall | Threat FPR | "
             "Weapon F1 | Fire F1 | Assault F1 |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for tag in tags:
        rep = _load(tag, "frames.json")
        if not rep:
            continue
        for split, m in rep["splits"].items():
            pc, td = m["per_class"], m["threat_detection"]

            def f1(c):
                return _f(pc[c]["f1"]) if pc[c]["support"] else "-"
            lines.append(f"| {tag} | {split} | {m['n']} | {_f(m['accuracy'])} | "
                         f"{_f(m['macro_f1'])} | {_f(td['recall'])} | "
                         f"{_f(td['false_positive_rate'])} | {f1('weapon')} | "
                         f"{f1('fire')} | {f1('assault')} |")
    return "\n".join(lines)


def replay_table(tags: list[str], report: str = "replay_all.json") -> str:
    lines = ["| Run | Videos | Ticks | Accuracy | Macro-F1 | Threat recall | "
             "Threat FPR | Episodes detected | False alarms / normal min |",
             "|---|---|---|---|---|---|---|---|---|"]
    for tag in tags:
        rep = _load(tag, report)
        if not rep:
            continue
        for split, m in rep["splits"].items():
            fr, ev = m["frames"], m["events"]
            td = fr["threat_detection"]
            lines.append(f"| {tag} | {split} | {fr['n']} | {_f(fr['accuracy'])} | "
                         f"{_f(fr['macro_f1'])} | {_f(td['recall'])} | "
                         f"{_f(td['false_positive_rate'])} | "
                         f"{ev['detected']}/{ev['episodes']} | "
                         f"{ev['false_alarms_per_normal_minute']:.2f} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    tags = (argv if argv is not None else sys.argv[1:]) or sorted(
        p.name for p in RUNS.iterdir() if p.is_dir() and not p.name.startswith("_"))
    print("### Per-image evaluation\n")
    print(frame_table(tags))
    print("\n### Video replay (end to end)\n")
    print(replay_table(tags))
    return 0


if __name__ == "__main__":
    sys.exit(main())
