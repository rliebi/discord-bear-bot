import json
import os
import threading
from typing import Any, Dict, Optional, List, Tuple
from datetime import datetime, timezone

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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        # Usage stats container per guild
        g.setdefault("usage", {})  # { user_id: {count, last_use_ts, ...} }
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
            g.setdefault("usage", {})
            data[gid] = g
            _write_all(data)
            return int(user_id)
        return int(g.get("admin_user_id")) if g.get("admin_user_id") is not None else None


def record_usage_event(
    guild_id: int,
    user_id: int,
    user_display: str,
    total_archers: int,
    march_count: int,
    calling: bool,
    joining_archers: int,
    calling_archers: int,
    server_id: int,
    server_name: str,
    server_max_troop_size: int,
) -> None:
    """Record a usage event for a user in a guild.

    Stores:
    - count of uses
    - last use timestamp (UTC ISO)
    - last input/output snapshot including archers and derived data
    - last server metadata
    """
    gid = str(guild_id)
    uid = str(user_id)
    with _lock:
        data = _read_all()
        g = data.get(gid) or {}
        usage = g.get("usage") or {}
        u = usage.get(uid) or {}
        u["count"] = int(u.get("count", 0)) + 1
        u["last_use_ts"] = _now_iso()
        u["user_display"] = user_display
        u["last_total_archers"] = int(total_archers)
        u["last_march_count"] = int(march_count)
        u["last_calling"] = bool(calling)
        u["last_joining_archers"] = int(joining_archers)
        u["last_calling_archers"] = int(calling_archers)
        u["last_server_id"] = int(server_id)
        u["last_server_name"] = server_name
        u["last_server_max_troop_size"] = int(server_max_troop_size)
        usage[uid] = u
        g["usage"] = usage
        data[gid] = g
        _write_all(data)


def get_usage_summary(guild_id: int, limit: int = 20) -> List[Tuple[int, Dict[str, Any]]]:
    """Return a list of (user_id, info) sorted by usage count desc for the guild."""
    gid = str(guild_id)
    with _lock:
        data = _read_all()
        g = data.get(gid) or {}
        usage = g.get("usage") or {}
        items: List[Tuple[int, Dict[str, Any]]] = []
        for k, v in usage.items():
            try:
                items.append((int(k), dict(v)))
            except Exception:
                continue
        items.sort(key=lambda x: int(x[1].get("count", 0)), reverse=True)
        if limit > 0:
            items = items[:limit]
        return items


def get_user_usage(guild_id: int, user_id: int) -> Optional[Dict[str, Any]]:
    gid = str(guild_id)
    uid = str(user_id)
    with _lock:
        data = _read_all()
        g = data.get(gid) or {}
        usage = g.get("usage") or {}
        if uid in usage:
            return dict(usage[uid])
        return None
