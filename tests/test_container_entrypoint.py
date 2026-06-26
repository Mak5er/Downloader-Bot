from __future__ import annotations

from pathlib import Path

import container_entrypoint


def test_resolve_command_uses_default_when_empty():
    assert container_entrypoint._resolve_command([]) == ["python", "main.py"]


def test_chown_tree_creates_and_visits_nested_paths(tmp_path, monkeypatch):
    target = tmp_path / "downloads"
    nested = target / "nested"
    nested.mkdir(parents=True)
    leaf = nested / "video.mp4"
    leaf.write_text("data", encoding="utf-8")

    calls: list[tuple[Path, int, int]] = []

    def fake_chown(path, *, uid, gid):
        calls.append((Path(path), uid, gid))
        return True

    monkeypatch.setattr(container_entrypoint, "_chown_path", fake_chown)

    container_entrypoint._chown_tree(target, uid=123, gid=456)

    touched_paths = {path for path, _, _ in calls}
    assert target in touched_paths
    assert nested in touched_paths
    assert leaf in touched_paths
    assert target.stat().st_mode & 0o700 == 0o700
    assert leaf.stat().st_mode & 0o600 == 0o600


def test_chown_tree_falls_back_to_world_writable_when_chown_is_denied(tmp_path, monkeypatch):
    target = tmp_path / "cookies"
    target.mkdir()
    cookie_file = target / "youtube.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    cookie_file.chmod(0o400)

    def fake_chown(_path, *, uid, gid):
        _ = (uid, gid)
        return False

    monkeypatch.setattr(container_entrypoint, "_chown_path", fake_chown)

    container_entrypoint._chown_tree(target, uid=123, gid=456)

    assert target.stat().st_mode & 0o777 == 0o777
    assert cookie_file.stat().st_mode & 0o666 == 0o666


def test_prepare_runtime_paths_manages_cookie_mount(monkeypatch):
    prepared_paths = []

    def fake_chown_tree(path, *, uid, gid):
        prepared_paths.append((str(path), uid, gid))

    monkeypatch.setattr(container_entrypoint, "_chown_tree", fake_chown_tree)

    container_entrypoint._prepare_runtime_paths(uid=123, gid=456)

    assert ("/app/cookies", 123, 456) in prepared_paths


def test_prepare_youtube_cookie_file_copies_inaccessible_mount(tmp_path, monkeypatch):
    source = tmp_path / "cookies" / "youtube.txt"
    source.parent.mkdir()
    source.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    source.chmod(0o600)
    runtime_cookie = tmp_path / ".runtime" / "cookies" / "youtube.txt"

    monkeypatch.setenv("YTDLP_YOUTUBE_COOKIES_FILE", str(source))
    monkeypatch.setattr(container_entrypoint, "RUNTIME_YOUTUBE_COOKIES_FILE", runtime_cookie)
    monkeypatch.setattr(container_entrypoint, "_chown_path", lambda _path, *, uid, gid: True)

    container_entrypoint._prepare_youtube_cookie_file(uid=123, gid=456)

    assert runtime_cookie.read_text(encoding="utf-8") == "# Netscape HTTP Cookie File\n"
    assert runtime_cookie.stat().st_mode & 0o600 == 0o600
    assert container_entrypoint.os.environ["YTDLP_YOUTUBE_COOKIES_FILE"] == str(runtime_cookie)
