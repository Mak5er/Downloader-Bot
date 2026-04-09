from __future__ import annotations

import os
import sys
from pathlib import Path

DEFAULT_COMMAND = ["python", "main.py"]
MANAGED_PATHS = ("/app/downloads", "/app/logs")


def _resolve_command(argv: list[str]) -> list[str]:
    return argv or list(DEFAULT_COMMAND)


def _chown_path(path: Path, *, uid: int, gid: int) -> None:
    chown = getattr(os, "chown", None)
    if chown is None:
        return
    chown(path, uid, gid)


def _chown_tree(path: Path, *, uid: int, gid: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _chown_path(path, uid=uid, gid=gid)

    for root, dirs, files in os.walk(path):
        root_path = Path(root)
        _chown_path(root_path, uid=uid, gid=gid)
        for name in dirs:
            _chown_path(root_path / name, uid=uid, gid=gid)
        for name in files:
            _chown_path(root_path / name, uid=uid, gid=gid)


def _prepare_runtime_paths(*, uid: int, gid: int) -> None:
    for raw_path in MANAGED_PATHS:
        _chown_tree(Path(raw_path), uid=uid, gid=gid)


def _drop_privileges(*, user_name: str, group_name: str) -> None:
    import grp
    import pwd

    user = pwd.getpwnam(user_name)
    group = grp.getgrnam(group_name)

    os.setgroups([group.gr_gid])
    os.setgid(group.gr_gid)
    os.setuid(user.pw_uid)

    os.environ["HOME"] = user.pw_dir
    os.environ["USER"] = user.pw_name


def main(argv: list[str] | None = None) -> int:
    command = _resolve_command(sys.argv[1:] if argv is None else argv)

    if os.geteuid() == 0:
        import grp
        import pwd

        user = pwd.getpwnam("appuser")
        group = grp.getgrnam("appgroup")
        _prepare_runtime_paths(uid=user.pw_uid, gid=group.gr_gid)
        _drop_privileges(user_name=user.pw_name, group_name=group.gr_name)

    os.execvp(command[0], command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
