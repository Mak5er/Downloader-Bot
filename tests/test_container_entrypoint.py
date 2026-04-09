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

    monkeypatch.setattr(container_entrypoint, "_chown_path", fake_chown)

    container_entrypoint._chown_tree(target, uid=123, gid=456)

    touched_paths = {path for path, _, _ in calls}
    assert target in touched_paths
    assert nested in touched_paths
    assert leaf in touched_paths
