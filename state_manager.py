"""Atomic JSON state I/O shared by every script in this project.
All state/config writes must go through here: temp file + os.replace, never direct write.
"""
import json
import os
import tempfile
from pathlib import Path


def atomic_write_json(path, data, indent=2):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def load_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def update_state(state_path, **updates):
    """Merge top-level keys into the state file and write it back atomically."""
    state = load_json(state_path, default={}) or {}
    state.update(updates)
    atomic_write_json(state_path, state)
    return state


def mark_unit(state_path, unit_name, status, **extra):
    """Set state['units'][unit_name] = {status, **extra} and write atomically."""
    state = load_json(state_path, default={}) or {}
    units = state.setdefault("units", {})
    units[unit_name] = {"status": status, **extra}
    atomic_write_json(state_path, state)
    return state
