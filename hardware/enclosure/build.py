"""Export every SENTRIX enclosure part as STL and 3MF, render the pictures, and
optionally run the interference checks.

    python hardware/enclosure/build.py            # parts + pictures
    python hardware/enclosure/build.py --check    # also the interference checks (slow, ~5 min)

Needs OpenSCAD 2021.01 or newer (openscad.org). Outputs go to
hardware/enclosure/stl/, 3mf/ and renders/.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODEL = HERE / "sentrix_enclosure.scad"
PARTS = ("base", "lid", "bracket", "pod", "pod_back", "coupon")
RENDERS = {   # name: (PART, extra -D, camera eye,centre, image size)
    "1_assembled":   ("assembly", {}, "220,-190,180,45,30,30", "1600,1200"),
    "2_exploded":    ("exploded", {}, "520,-560,420,44,30,140", "1400,1600"),
    "3_camera_end":  ("assembly", {}, "200,40,70,85,30,30", "1400,1050"),
    "4_button_end":  ("assembly", {"TILT": 0}, "-130,-80,70,10,25,18", "1400,1050"),
    "5_lid_inside":  ("lid", {}, "42,-70,170,42,-32,0", "1400,1050"),
    "6_base_inside": ("base", {}, "42,-60,190,42,32,10", "1400,1050"),
    "7_wall_mount":  ("base", {}, "40,-120,-160,42,32,0", "1400,1050"),
    "8_print_plate": ("print_plate", {}, "40,-40,330,40,90,0", "1600,1200"),
    "9_wall_mounted": ("wall", {}, "250,-270,90,32,-30,-40", "1400,1200"),
}
CHECKS = {   # name: OpenSCAD expression whose result must be empty
    "base_vs_pi": "intersection() { base(); pi5_envelope(); }",
    "lid_vs_pi": "intersection() { lid(); pi5_envelope(); }",
    "buzzer_vs_pi": "intersection() { buzzer(); pi5_envelope(); }",
    "leds_vs_pi": "intersection() { leds5_envelope(); pi5_envelope(); }",
    "leds_vs_buzzer": "intersection() { leds5_envelope(); buzzer(); }",    "pod_vs_camera": "intersection() { pod(); camera3_envelope(); }",
    "back_vs_camera": "intersection() { pod_back(); camera3_envelope(); }",
    # mating faces touch on purpose; a 0.02 mm shift keeps a real lip collision visible
    "back_vs_pod": "intersection() { translate([-0.02, 0, 0]) pod_back(); pod(); }",
    "ribbon_vs_pod": "intersection() { pod(); ribbon_envelope(); }",
    "ribbon_vs_back": "intersection() { pod_back(); ribbon_envelope(); }",
    "bolt_vs_ribbon": "intersection() { pivot_bolt(); ribbon_envelope(); }",
    "bolt_vs_camera": "intersection() { pivot_bolt(); camera3_envelope(); }",
    "bolt_vs_pod": "intersection() { pivot_bolt(); pod(); }",
    "lid_vs_base": "intersection() { lid(); translate([0, 0, -0.01]) base(); }",
    **{f"tilt_{a}": f"intersection() {{ pod_placed({a}) {{ pod(); pod_back(); }} "
                    f"union() {{ bracket_no_detents(); lid(); }} }}" for a in range(0, 91, 15)},
}


def openscad() -> str:
    for candidate in (shutil.which("openscad"), r"C:\Program Files\OpenSCAD\openscad.com",
                      r"C:\Program Files\OpenSCAD\openscad.exe",
                      "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD"):
        if candidate and Path(candidate).exists():
            return candidate
    sys.exit("OpenSCAD not found: install it from openscad.org")


def run(args: list[str]) -> tuple[int, str]:
    done = subprocess.run([openscad(), *args], capture_output=True, text=True)
    return done.returncode, done.stdout + done.stderr


def defines(values: dict) -> list[str]:
    out = []
    for key, value in values.items():
        out += ["-D", f'{key}="{value}"' if isinstance(value, str) else f"{key}={value}"]
    return out


def export(part: str) -> str:
    lines = []
    for ext in ("stl", "3mf"):
        target = HERE / ext / f"sentrix_{part}.{ext}"
        target.parent.mkdir(exist_ok=True)
        code, log = run(["-o", str(target), *defines({"PART": part}), str(MODEL)])
        lines.append(f"{target.relative_to(HERE)}: {'ok' if code == 0 and target.exists() else 'FAILED'}")
        if code:
            lines.append(log[-400:])
    return "\n".join(lines)


def render(name: str, spec) -> str:
    part, extra, camera, size = spec
    target = HERE / "renders" / f"{name}.png"
    target.parent.mkdir(exist_ok=True)
    code, log = run(["-o", str(target), *defines({"PART": part, **extra}), f"--camera={camera}",
                     f"--imgsize={size}", "--colorscheme=Tomorrow", "--projection=p", str(MODEL)])
    return f"{target.relative_to(HERE)}: {'ok' if code == 0 else 'FAILED ' + log[-200:]}"


def check(name: str, expression: str, workdir: Path) -> str:
    source = workdir / f"{name}.scad"
    source.write_text(f'include <{MODEL.as_posix()}>\nPART = "none";\n{expression}\n')
    code, log = run(["-o", str(workdir / f"{name}.stl"), str(source)])
    if "top level object is empty" in log.lower():
        return f"{name}: clear"
    return f"{name}: OVERLAP (see {workdir / (name + '.stl')})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="run the interference checks too")
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    args = parser.parse_args(argv)
    with cf.ThreadPoolExecutor(args.jobs) as pool:
        for result in pool.map(export, PARTS):
            print(result)
        for result in pool.map(lambda kv: render(*kv), RENDERS.items()):
            print(result)
        if args.check:
            workdir = Path(tempfile.mkdtemp(prefix="sentrix-checks-"))
            results = list(pool.map(lambda kv: check(kv[0], kv[1], workdir), CHECKS.items()))
            print("\n".join(sorted(results)))
            return 0 if all(r.endswith("clear") for r in results) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
