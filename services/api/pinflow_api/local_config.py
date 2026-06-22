"""Tiny persistent key-value store for machine-local user preferences.

Distinct from `settings.py` (env / .env — read-only process config) and the
frontend `lib/config` (localStorage). This holds runtime-settable values that
must (a) survive a restart and (b) live on the machine running the local agent
— e.g. the KiCad symbol-library directory override, which is inherently
per-install and can't be baked into shipped config. Backed by a JSON file at
`~/.pinflow/config.json`.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

_CONFIG_PATH = Path.home() / ".pinflow" / "config.json"
_lock = threading.Lock()


def _read() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def get(key: str, default: Any = None) -> Any:
    return _read().get(key, default)


def set(key: str, value: Any) -> None:
    """Persist `value` under `key` (atomic write). `value=None` removes the key."""
    with _lock:
        data = _read()
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(_CONFIG_PATH)
