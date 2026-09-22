from __future__ import annotations

"""SHA-256 hash-keyed scan result cache for RADIANT-LLM user skill packs.

Cache file: {skills_directory}/.radiant_scan_cache.json
Every get_entry() re-hashes the current SKILL.md body so a tampered cache
file cannot grant a modified skill a free pass.
"""

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional

CACHE_VERSION = 2
_lock = threading.Lock()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _cache_path(skills_directory: str | Path) -> Path:
    return Path(skills_directory) / ".radiant_scan_cache.json"


def load_cache(skills_directory: str | Path) -> Dict[str, Any]:
    path = _cache_path(skills_directory)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != CACHE_VERSION:
            return {"version": CACHE_VERSION, "entries": {}}
        return data
    except Exception:
        return {"version": CACHE_VERSION, "entries": {}}


def save_cache(cache: Dict[str, Any], skills_directory: str | Path) -> None:
    path = _cache_path(skills_directory)
    try:
        with _lock:
            path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except Exception:
        pass


def get_entry(
    cache: Dict[str, Any],
    pack_name: str,
    current_hash: str,
) -> Optional[Dict[str, Any]]:
    """Return the cached entry for pack_name only if the hash still matches."""
    entry = cache.get("entries", {}).get(pack_name)
    if not entry:
        return None
    if entry.get("hash") != current_hash:
        return None
    return entry


def put_entry(
    cache: Dict[str, Any],
    pack_name: str,
    current_hash: str,
    result: Dict[str, Any],
) -> None:
    cache.setdefault("entries", {})[pack_name] = {"hash": current_hash, **result}
