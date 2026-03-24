import json
import os
import threading
from typing import Any, Dict, Optional

_DATA_DIR = os.environ.get("DATA_DIR", "/data")
_SETTINGS_FILE = os.path.join(_DATA_DIR, "guild_settings.json")
_lock = threading.Lock()


def _ensure_storage():
    os.makedirs(_DATA_DIR, exist_ok=True)
    if not os.path.exists(_SETTINGS_FILE):
        with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)


def _read_all() -> Dict[str, Any]:
    _ensure_storage()
    with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def _write_all(data: Dict[str, Any]) -> None:
    tmp = _SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, _SETTINGS_FILE)


def get_guild_settings(guild_id: int) -> Dict[str, Any]:
    """Return settings dict for a guild, creating defaults if not present."""
    gid = str(guild_id)
    with _lock:
        data = _read_all()
        g = data.get(gid) or {}
        # Defaults
        g.setdefault("admin_user_id", None)
        g.setdefault("max_troop_size", 0)
        g.setdefault("infantry_amount", 0)
        g.setdefault("max_archers_amount", 0)
        g.setdefault("calc_message", "")
        g.setdefault("message_ttl_minutes", 10)
        data[gid] = g
        _write_all(data)
        return g


def update_guild_settings(guild_id: int, updates: Dict[str, Any]) -> Dict[str, Any]:
    gid = str(guild_id)
    with _lock:
        data = _read_all()
        g = data.get(gid) or {}
        g.update(updates)
        data[gid] = g
        _write_all(data)
        return g


def set_admin_if_unset(guild_id: int, user_id: int) -> Optional[int]:
    """If admin not set for guild, set to given user_id. Return admin id after call."""
    gid = str(guild_id)
    with _lock:
        data = _read_all()
        g = data.get(gid) or {}
        if g.get("admin_user_id") is None:
            g["admin_user_id"] = int(user_id)
            g.setdefault("max_troop_size", 0)
            g.setdefault("infantry_amount", 0)
            g.setdefault("max_archers_amount", 0)
            g.setdefault("calc_message", "")
            g.setdefault("message_ttl_minutes", 10)
            data[gid] = g
            _write_all(data)
            return int(user_id)
        return int(g.get("admin_user_id")) if g.get("admin_user_id") is not None else None
