from __future__ import annotations

import copy
import json
from pathlib import Path
from threading import RLock
from typing import Any, Callable


_STATE_FILE = Path(__file__).resolve().parents[1] / ".runtime_state.json"
_STATE_LOCK = RLock()


def _load_state_unlocked() -> dict[str, Any]:
    if not _STATE_FILE.exists():
        return {}

    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state_unlocked(state: dict[str, Any]) -> None:
    if not state:
        if _STATE_FILE.exists():
            _STATE_FILE.unlink()
        return

    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _STATE_FILE.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(state, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
    temp_path.replace(_STATE_FILE)


def load_bucket(name: str, default_factory: Callable[[], Any]) -> Any:
    with _STATE_LOCK:
        state = _load_state_unlocked()
        value = state.get(name)
        if value is None:
            return default_factory()
        return copy.deepcopy(value)


def save_bucket(name: str, value: Any) -> None:
    with _STATE_LOCK:
        state = _load_state_unlocked()
        if value:
            state[name] = copy.deepcopy(value)
        else:
            state.pop(name, None)
        _write_state_unlocked(state)
