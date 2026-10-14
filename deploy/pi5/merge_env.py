"""Merge ``deploy/pi5/pi.env`` into ``.env`` (used by ``install_pi.sh``).

Rules: a key ``.env`` already sets keeps its value; a key it lacks is
appended under a "Raspberry Pi 5 defaults" heading. With ``--fresh`` (the
installer just created ``.env`` from ``.env.example``) the template's values
are replaced by the Pi defaults instead, so a new unit starts on the CSI
camera rather than webcam 0. Secrets are never touched: ``pi.env`` has none.

    python deploy/pi5/merge_env.py .env deploy/pi5/pi.env [--fresh]

Prints the keys it set, one per line.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HEADING = "# ─── Raspberry Pi 5 defaults (deploy/pi5/pi.env) ───"
_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def parse_defaults(text: str) -> dict[str, str]:
    """``KEY=value`` lines of ``pi.env`` in order (comments skipped)."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _LINE.match(line.strip())
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def merge(env_text: str, defaults: dict[str, str], *, fresh: bool = False
          ) -> tuple[str, list[str]]:
    """The new ``.env`` text and the keys that were set."""
    lines = env_text.splitlines()
    present = {m.group(1): i for i, line in enumerate(lines)
               if (m := _LINE.match(line.strip()))}
    changed: list[str] = []
    appended: list[str] = []
    for key, value in defaults.items():
        if key in present:
            index = present[key]
            current = _LINE.match(lines[index].strip()).group(2).strip()
            if fresh and current != value:
                lines[index] = f"{key}={value}"
                changed.append(key)
            continue
        appended.append(f"{key}={value}")
        changed.append(key)
    if appended:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(HEADING)
        lines.extend(appended)
    return "\n".join(lines) + "\n", changed


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 2:
        print("usage: merge_env.py ENV_FILE DEFAULTS_FILE [--fresh]", file=sys.stderr)
        return 2
    env_path, defaults_path = Path(args[0]), Path(args[1])
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    new, changed = merge(text, parse_defaults(defaults_path.read_text(encoding="utf-8")),
                         fresh="--fresh" in argv)
    if changed:
        env_path.write_text(new, encoding="utf-8")
    for key in changed:
        print(key)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
