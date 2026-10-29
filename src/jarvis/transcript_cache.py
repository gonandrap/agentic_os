"""Parsed transcript products, kept per file and reused until that file changes"""
from __future__ import annotations

import gzip
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

from jarvis import paths

#: Bumped whenever a product's shape changes, which makes every older file unreadable
VERSION = 1
MAX_FILES = 400

_lock = threading.RLock()
_files: dict[str, dict[str, Any]] = {}
_clock = 0
_loaded = False


def cache_path() -> Path:
    return paths.jarvis_home() / "cache" / f"transcripts-v{VERSION}.json.gz"


def _stat(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def product(path: Path | str, name: str, build: Callable[[], Any]) -> Any:
    """`name` for this transcript, built by `build` only if no live entry has it"""
    global _clock
    path = Path(path)
    stat = _stat(path)
    if stat is None:
        return build()
    load()
    key = str(path)
    with _lock:
        entry = _files.get(key)
        if entry is None or tuple(entry["stat"]) != stat:
            # A transcript only ever grows, so a changed size or mtime means new rows
            entry = _files[key] = {"stat": list(stat), "products": {}, "used": 0}
        _clock += 1
        entry["used"] = _clock
        if name in entry["products"]:
            return entry["products"][name]
    value = build()
    with _lock:
        entry = _files.get(key)
        # Dropped by an eviction, or superseded while `build` ran: the value is still correct
        if entry is not None and tuple(entry["stat"]) == stat:
            entry["products"][name] = value
            _evict()
    return value


def _evict() -> None:
    while len(_files) > MAX_FILES:
        oldest = min(_files, key=lambda k: _files[k]["used"])
        del _files[oldest]


def load() -> None:
    """The persisted products, once per process; a damaged or older file is ignored"""
    global _loaded
    with _lock:
        if _loaded:
            return
        _loaded = True
        try:
            with gzip.open(cache_path(), "rt", encoding="utf-8") as handle:
                saved = json.load(handle)
        except (OSError, ValueError):
            return
        if not isinstance(saved, dict) or saved.get("version") != VERSION:
            return
        files = saved.get("files")
        if isinstance(files, dict):
            for key, entry in files.items():
                if isinstance(entry, dict) and isinstance(entry.get("stat"), list):
                    entry.setdefault("products", {})
                    entry["used"] = 0
                    _files[key] = entry
            _evict()


def save() -> None:
    """Write the cache out; ONE writer only, because the last one wins the file"""
    with _lock:
        payload = {"version": VERSION, "files": _files}
        blob = json.dumps(payload).encode()
    target = cache_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(f".{os.getpid()}.tmp")
    try:
        with gzip.open(tmp, "wb") as handle:
            handle.write(blob)
        os.replace(tmp, target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def reset() -> None:
    """Forget every in-memory entry, so the next read loads or rebuilds — for tests"""
    global _loaded, _clock
    with _lock:
        _files.clear()
        _clock = 0
        _loaded = False


def stats() -> dict[str, int]:
    with _lock:
        return {"files": len(_files),
                "products": sum(len(e["products"]) for e in _files.values())}
