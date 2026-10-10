"""Create or complete ``.env`` for a first run.

Copies ``.env.example`` to ``.env`` if there is none, then fills every blank
secret with a random value: the dashboard password (printed once, so you can
log in), the session-signing secret and the evidence-encryption key. A blank
evidence key used to leave every evidence bundle unreadable after a restart.
Existing values are never changed.

Exit codes: 0 ready, 2 the dashboard password is still blank.

Usage::

    python tools/setup_env.py
"""

from __future__ import annotations

import re
import secrets
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: key -> generator for its value when blank.
GENERATED = {
    "SENTRIX_PASSWORD": lambda: secrets.token_urlsafe(12),
    "SESSION_SECRET": lambda: secrets.token_hex(32),
    "EVIDENCE_AES_KEY": lambda: secrets.token_hex(32),
}


def fill(text: str) -> tuple[str, dict[str, str]]:
    """Fill blank ``KEY=`` lines; returns the new text and what was generated."""
    made: dict[str, str] = {}
    for key, make in GENERATED.items():
        pattern = re.compile(rf"(?m)^{key}=[ \t]*$")
        if pattern.search(text):
            value = make()
            text = pattern.sub(f"{key}={value}", text, count=1)
            made[key] = value
        elif not re.search(rf"(?m)^{key}=", text):
            value = make()
            text = text.rstrip("\n") + f"\n{key}={value}\n"
            made[key] = value
    return text, made


def main(argv: list[str] | None = None) -> int:
    env, example = ROOT / ".env", ROOT / ".env.example"
    if not env.exists():
        shutil.copyfile(example, env)
        print(f"created {env.name} from {example.name}")
    text, made = fill(env.read_text(encoding="utf-8"))
    if made:
        env.write_text(text, encoding="utf-8")
        for key in made:
            if key != "SENTRIX_PASSWORD":
                print(f"generated {key}")
    if "SENTRIX_PASSWORD" in made:
        print()
        print(f"  Dashboard password: {made['SENTRIX_PASSWORD']}")
        print("  (stored in .env as SENTRIX_PASSWORD; change it there if you like)")
        print()
    password = re.search(r"(?m)^SENTRIX_PASSWORD=(.*)$", text)
    if not password or not password.group(1).strip():
        print("SENTRIX_PASSWORD in .env is blank: set it, then start again.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
