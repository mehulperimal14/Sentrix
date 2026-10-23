"""Container entry point: make the data volume writable, then run the Portal
as the unprivileged ``sentrix`` user.

Railway and Render mount a volume root-owned over ``/data``, which the image
prepared for ``sentrix``. Started as root, this script creates
``/data/portal`` and ``/data/var``, hands ``/data`` to ``sentrix``, drops
root for good (groups, gid, uid) and only then executes the command (the
image's ``CMD``: ``python -m sentrix_portal``). Started as any other user
(``docker run --user``), it executes the command unchanged.
"""

from __future__ import annotations

import os
import sys

USER = "sentrix"
DATA = os.environ.get("SENTRIX_DATA_ROOT", "/data")


def take_ownership(root: str, uid: int, gid: int) -> int:
    """``chown -R uid:gid root``, never following links, each path once;
    returns the count."""
    count = 0

    def own(path: str) -> None:
        nonlocal count
        try:
            os.chown(path, uid, gid, follow_symlinks=False)
            count += 1
        except OSError:
            pass

    own(root)
    for directory, dirs, files in os.walk(root):
        for name in dirs + files:
            own(os.path.join(directory, name))
    return count


def drop_to(user: str) -> None:
    import pwd
    entry = pwd.getpwnam(user)
    for sub in ("portal", "var"):
        os.makedirs(os.path.join(DATA, sub), exist_ok=True)
    take_ownership(DATA, entry.pw_uid, entry.pw_gid)
    os.setgroups([])
    os.setgid(entry.pw_gid)
    os.setuid(entry.pw_uid)
    if os.getuid() == 0 or os.geteuid() == 0:
        raise SystemExit("start.py: could not drop root")
    os.environ["HOME"] = entry.pw_dir
    os.environ["USER"] = user


def main(argv: list[str]) -> None:
    command = argv or ["python", "-m", "sentrix_portal"]
    if hasattr(os, "getuid") and os.getuid() == 0:
        drop_to(USER)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main(sys.argv[1:])
