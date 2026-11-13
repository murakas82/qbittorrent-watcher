"""Validated JSON storage and transactions for watch settings and downloads."""

import copy
import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from matching import normalize

try:
    import msvcrt
except ImportError:
    msvcrt = None
    import fcntl

HERE = Path(__file__).resolve().parent
SCHEMA_VERSION = 2


class FileLock:
    """A lock shared between processes, held through an open file.

    The operating system releases it when the holder exits, even if the
    holder crashed or was killed, so it can never be left behind and it never
    expires while its holder is still working.
    """

    def __init__(self, path):
        self.path = path
        self.handle = None

    def acquire(self, timeout=0):
        """Try to take the lock, waiting up to `timeout` seconds."""
        deadline = time.monotonic() + timeout
        handle = open(self.path, "a+b")
        while True:
            try:
                if msvcrt:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.handle = handle
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    return False
                time.sleep(0.05)

    def release(self):
        if self.handle is None:
            return
        try:
            if msvcrt:
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self.handle, fcntl.LOCK_UN)
        except OSError:
            pass
        self.handle.close()
        self.handle = None


SERIES_DEFAULTS = {
    "quality": "", "min_seeders": 1, "must_not_contain": [], "save_path": "",
    "enabled": True, "remove_when_done": False, "part_of_title": False,
}
SERIES_FIELDS = {"name", "season", "next_episode", *SERIES_DEFAULTS}


def empty_config():
    return {"schema_version": SCHEMA_VERSION,
            "qbittorrent_url": "http://127.0.0.1:8080",
            "search_timeout_seconds": 90, "series": [], "downloads": []}


def same_title(a, b):
    """Duplicate watch entries use the same normalized name and season."""
    return (normalize(a["name"]).replace(" ", "") ==
            normalize(b["name"]).replace(" ", "") and a["season"] == b["season"])


def _whole(value, label, minimum=1):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be a whole number >= {minimum}")


def validate(config):
    """Validate and supply defaults without changing the caller's data."""
    if not isinstance(config, dict):
        raise ValueError("series.json must contain an object")
    result = empty_config()
    result.update(copy.deepcopy(config))
    if type(result["schema_version"]) is not int or result["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Unsupported series.json schema version")
    if not isinstance(result["qbittorrent_url"], str) or not result["qbittorrent_url"].startswith(("http://", "https://")):
        raise ValueError("qbittorrent_url must be an HTTP or HTTPS URL")
    timeout = result["search_timeout_seconds"]
    if type(timeout) not in (int, float) or not 0 < timeout < float("inf"):
        raise ValueError("search_timeout_seconds must be positive")
    if "max_downloads" in result:
        _whole(result["max_downloads"], "max_downloads", 0)
    if "autostart" in result and type(result["autostart"]) is not bool:
        raise ValueError("autostart must be true or false")
    if not isinstance(result["series"], list) or not isinstance(result["downloads"], list):
        raise ValueError("series and downloads must be lists")
    seen = set()
    for show in result["series"]:
        if not isinstance(show, dict):
            raise ValueError("Each series must be an object")
        for key, value in SERIES_DEFAULTS.items():
            show.setdefault(key, copy.deepcopy(value))
        if not isinstance(show.get("id"), str) or not show["id"] or show["id"] in seen:
            raise ValueError("Every series needs a unique ID")
        seen.add(show["id"])
        if not isinstance(show.get("name"), str) or not show["name"].strip():
            raise ValueError("Every series needs a name")
        _whole(show.get("season"), "season")
        _whole(show.get("next_episode"), "next_episode")
        _whole(show["min_seeders"], "min_seeders", 0)
        for key in ("enabled", "remove_when_done", "part_of_title"):
            if type(show[key]) is not bool:
                raise ValueError(f"{key} must be true or false")
        for key in ("quality", "save_path"):
            if not isinstance(show[key], str):
                raise ValueError(f"{key} must be text")
        if not isinstance(show["must_not_contain"], list) or not all(isinstance(word, str) for word in show["must_not_contain"]):
            raise ValueError("must_not_contain must be a list of words")
        if "history" in show or "cleanup" in show:
            raise ValueError("Download history belongs in downloads")
    record_ids = set()
    for record in result["downloads"]:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"] or record["id"] in record_ids:
            raise ValueError("Every download needs a unique ID")
        record_ids.add(record["id"])
        if record.get("series_id") not in seen:
            raise ValueError("Download refers to an unknown series")
        for key in ("season", "episode"):
            if key not in record:
                raise ValueError(f"Download {key} is missing")
            if record.get(key) is not None:
                _whole(record[key], f"download {key}")
        for key in ("title", "added"):
            if not isinstance(record.get(key), str):
                raise ValueError(f"Download {key} must be text")
        if type(record.get("managed")) is not bool:
            raise ValueError("Download managed must be true or false")
        if record["managed"] and (not isinstance(record.get("hash"), str) or not record["hash"]):
            raise ValueError("Managed downloads need a torrent hash")
        if record.get("status") not in ("added", "removed", "observed"):
            raise ValueError("Unknown download status")
    return result


def upgrade_config(raw):
    """Convert the old per-season history and cleanup lists without data loss."""
    if not isinstance(raw, dict):
        raise ValueError("series.json must contain an object")
    version = raw.get("schema_version", 1)
    if type(version) is not int or version not in (1, SCHEMA_VERSION):
        raise ValueError(f"Unsupported series.json schema version: {version}")
    if version == SCHEMA_VERSION:
        return validate(raw)
    config = empty_config()
    config.update(copy.deepcopy(raw))
    config["schema_version"] = SCHEMA_VERSION
    config["downloads"] = []
    if not isinstance(config["series"], list):
        raise ValueError("series must be a list")
    for show in config["series"]:
        if not isinstance(show, dict):
            raise ValueError("Each series must be an object")
        show["id"] = show.get("id") or uuid.uuid4().hex
        history = show.pop("history", [])
        cleanup = show.pop("cleanup", [])
        if not isinstance(history, list) or not isinstance(cleanup, list):
            raise ValueError("Legacy history and cleanup must be lists")
        for item in history:
            if not isinstance(item, dict):
                raise ValueError("Legacy history entries must be objects")
            _whole(item.get("episode"), "history episode")
            record = copy.deepcopy(item)
            record.update(id=uuid.uuid4().hex, series_id=show["id"],
                          season=show.get("season"), managed=bool(item.get("hash")),
                          status="added" if item.get("hash") else "observed")
            record.setdefault("title", "")
            record.setdefault("added", "")
            config["downloads"].append(record)
        for item in cleanup:
            if not isinstance(item, dict) or not isinstance(item.get("hash"), str) or not item["hash"]:
                raise ValueError("Legacy cleanup entries need a torrent hash")
            if any(d.get("hash", "").lower() == item["hash"].lower() and d["series_id"] == show["id"] for d in config["downloads"]):
                continue
            label = item.get("label", "")
            if not isinstance(label, str):
                raise ValueError("Legacy cleanup labels must be text")
            tag = re.fullmatch(r"S(\d+)E(\d+)", label)
            config["downloads"].append(dict(
                id=uuid.uuid4().hex, series_id=show["id"],
                season=int(tag[1]) if tag else None, episode=int(tag[2]) if tag else None,
                title="", added=item.get("since", ""), hash=item["hash"],
                managed=True, status="added", label=label))
    return validate(config)


class ConfigStore:
    """The only writer of settings and download records.

    Migration happens under the same process lock as every update. The exact
    original bytes are backed up before the first schema upgrade is saved.
    """

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else HERE / "series.json"
        self.lock_path = self.path.with_suffix(".lock")

    def _read(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return empty_config()

    def _save(self, config):
        temp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        try:
            temp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            for attempt in range(20):
                try:
                    os.replace(temp, self.path)
                    return
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.05)
        finally:
            temp.unlink(missing_ok=True)

    def _load_locked(self):
        raw = self._read()
        config = upgrade_config(raw)
        if raw.get("schema_version", 1) != SCHEMA_VERSION:
            backup = self.path.with_name(self.path.name + ".v1.bak")
            if backup.exists():
                backup = self.path.with_name(f"{self.path.name}.v1.{time.time_ns()}.bak")
            with backup.open("xb") as target:
                target.write(self.path.read_bytes())
            self._save(config)
        return config

    def load(self):
        raw = self._read()
        if isinstance(raw, dict) and raw.get("schema_version") == SCHEMA_VERSION:
            return validate(raw)
        lock = FileLock(self.lock_path)
        if not lock.acquire(timeout=15):
            raise OSError("series.json is in use by another program")
        try:
            return self._load_locked()
        finally:
            lock.release()

    def update(self, change):
        lock = FileLock(self.lock_path)
        if not lock.acquire(timeout=15):
            raise OSError("series.json is in use by another program")
        try:
            config = self._load_locked()
            change(config)
            config = validate(config)
            self._save(config)
            return config
        finally:
            lock.release()

    def current_entry(self, series_id):
        return next((show for show in self.load()["series"] if show["id"] == series_id), None)

    def add_series(self, fields):
        show = {key: copy.deepcopy(value) for key, value in fields.items() if key in SERIES_FIELDS}
        show["id"] = uuid.uuid4().hex
        def add(config):
            if any(same_title(show, other) for other in config["series"]):
                raise ValueError("This series and season are already in the list")
            config["series"].append(show)
        self.update(add)
        return show["id"]

    def edit_series(self, series_id, changes, original=None):
        fields = {key: copy.deepcopy(value) for key, value in changes.items() if key in SERIES_FIELDS}
        # An untouched dialog field must not roll back progress made meanwhile.
        if original and fields.get("next_episode") == original["next_episode"] and fields.get("season", original["season"]) == original["season"]:
            fields.pop("next_episode", None)
        def edit(config):
            entry = next((s for s in config["series"] if s["id"] == series_id), None)
            if entry is None:
                raise ValueError("This series was removed")
            entry.update(fields)
            if any(s["id"] != series_id and same_title(entry, s) for s in config["series"]):
                raise ValueError("This series and season are already in the list")
        return self.update(edit)

    def toggle_series(self, series_id):
        def toggle(config):
            for entry in config["series"]:
                if entry["id"] == series_id:
                    entry["enabled"] = not entry["enabled"]
        return self.update(toggle)

    def remove_series(self, series_id):
        def remove(config):
            config["series"] = [s for s in config["series"] if s["id"] != series_id]
            config["downloads"] = [d for d in config["downloads"] if d["series_id"] != series_id]
        return self.update(remove)

    def set_setting(self, key, value):
        if key not in ("max_downloads", "autostart", "qbittorrent_url", "search_timeout_seconds"):
            raise ValueError(f"Unknown setting: {key}")
        return self.update(lambda config: config.__setitem__(key, value))

    def record_download(self, show, title, torrent_hash=None):
        """Keep each managed torrent independently, including replacements."""
        def record(config):
            entry = next((s for s in config["series"] if s["id"] == show["id"]), None)
            if entry is None:
                return
            fresh = dict(id=uuid.uuid4().hex, series_id=show["id"], season=show["season"],
                         episode=show["next_episode"], title=title or "",
                         added=f"{datetime.now():%Y-%m-%d %H:%M}", managed=bool(torrent_hash),
                         status="added" if torrent_hash else "observed")
            if torrent_hash:
                fresh["hash"] = torrent_hash.lower()
            existing = next((d for d in config["downloads"]
                             if d["series_id"] == show["id"] and d["season"] == fresh["season"]
                             and d["episode"] == fresh["episode"]
                             and d.get("hash") == fresh.get("hash")), None)
            if existing is None:
                config["downloads"].append(fresh)
            elif torrent_hash:
                fresh["id"] = existing["id"]
                existing.update(fresh)
            if entry["season"] == show["season"] and entry["next_episode"] == show["next_episode"]:
                entry["next_episode"] += 1
        return self.update(record)

    def mark_removed(self, record_ids):
        def mark(config):
            for record in config["downloads"]:
                if record["id"] in record_ids:
                    record["status"] = "removed"
        return self.update(mark)
