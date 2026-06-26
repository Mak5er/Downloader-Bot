from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

DEFAULT_COMMAND = ["python", "main.py"]
MANAGED_PATHS = ("/app/downloads", "/app/logs", "/app/cookies")
DEFAULT_YOUTUBE_COOKIES_FILE = Path("/app/cookies/youtube.txt")
RUNTIME_YOUTUBE_COOKIES_FILE = Path("/app/.runtime/cookies/youtube.txt")


def _resolve_command(argv: list[str]) -> list[str]:
    return argv or list(DEFAULT_COMMAND)


def _chown_path(path: Path, *, uid: int, gid: int) -> bool:
    chown = getattr(os, "chown", None)
    if chown is None:
        return False
    try:
        chown(path, uid, gid)
    except PermissionError:
        return False
    return True


def _ensure_runtime_access(path: Path, *, owner_only: bool) -> None:
    if owner_only:
        access_mode = 0o700 if path.is_dir() else 0o600
    else:
        access_mode = 0o777 if path.is_dir() else 0o666
    try:
        os.chmod(path, access_mode)
    except PermissionError:
        return


def _chown_tree(path: Path, *, uid: int, gid: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _ensure_runtime_access(path, owner_only=_chown_path(path, uid=uid, gid=gid))

    for root, dirs, files in os.walk(path):
        root_path = Path(root)
        _ensure_runtime_access(root_path, owner_only=_chown_path(root_path, uid=uid, gid=gid))
        for name in dirs:
            child = root_path / name
            _ensure_runtime_access(child, owner_only=_chown_path(child, uid=uid, gid=gid))
        for name in files:
            child = root_path / name
            _ensure_runtime_access(child, owner_only=_chown_path(child, uid=uid, gid=gid))


def _prepare_runtime_paths(*, uid: int, gid: int) -> None:
    for raw_path in MANAGED_PATHS:
        _chown_tree(Path(raw_path), uid=uid, gid=gid)


def _path_allows_user_read_write(path: Path, *, uid: int, gid: int) -> bool:
    stat_result = path.stat()
    mode = stat_result.st_mode
    if stat_result.st_uid == uid:
        return mode & 0o600 == 0o600
    if stat_result.st_gid == gid:
        return mode & 0o060 == 0o060
    return mode & 0o006 == 0o006


def _resolve_youtube_cookie_source() -> Path:
    configured_path = os.getenv("YTDLP_YOUTUBE_COOKIES_FILE")
    if configured_path and configured_path.strip():
        source = Path(configured_path.strip())
        return source if source.is_absolute() else Path("/app") / source
    return DEFAULT_YOUTUBE_COOKIES_FILE


def _prepare_youtube_cookie_file(*, uid: int, gid: int) -> None:
    source = _resolve_youtube_cookie_source()
    if not source.is_file() or _path_allows_user_read_write(source, uid=uid, gid=gid):
        return

    RUNTIME_YOUTUBE_COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, RUNTIME_YOUTUBE_COOKIES_FILE)
    _chown_path(RUNTIME_YOUTUBE_COOKIES_FILE.parent, uid=uid, gid=gid)
    _ensure_runtime_access(RUNTIME_YOUTUBE_COOKIES_FILE.parent, owner_only=True)
    _chown_path(RUNTIME_YOUTUBE_COOKIES_FILE, uid=uid, gid=gid)
    _ensure_runtime_access(RUNTIME_YOUTUBE_COOKIES_FILE, owner_only=True)
    os.environ["YTDLP_YOUTUBE_COOKIES_FILE"] = str(RUNTIME_YOUTUBE_COOKIES_FILE)


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
        _prepare_youtube_cookie_file(uid=user.pw_uid, gid=group.gr_gid)
        _drop_privileges(user_name=user.pw_name, group_name=group.gr_name)

    os.execvp(command[0], command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
