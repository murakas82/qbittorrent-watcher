"""qBittorrent Web API transport and client-specific preferences."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class QBittorrent:
    def __init__(self, base_url):
        self.base = base_url.rstrip("/") + "/api/v2/"
        self._api_version = None

    def call(self, endpoint, params=None, post=False, timeout=30):
        data = urllib.parse.urlencode(params or {})
        url = self.base + endpoint
        if post:
            request = urllib.request.Request(url, data=data.encode("utf-8"))
        else:
            request = urllib.request.Request(url + ("?" + data if data else ""))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")

    def search(self, pattern, timeout):
        """Run a search with all enabled plugins and return the result rows."""
        job = json.loads(
            self.call(
                "search/start",
                {"pattern": pattern, "plugins": "enabled", "category": "all"},
                post=True,
            )
        )["id"]
        try:
            deadline = time.time() + timeout
            while time.time() < deadline:
                status = json.loads(self.call("search/status", {"id": job}))
                if status and status[0]["status"] != "Running":
                    break
                time.sleep(2)
            else:
                self.call("search/stop", {"id": job}, post=True)
            reply = json.loads(
                self.call("search/results", {"id": job, "limit": 500, "offset": 0})
            )
            return reply.get("results", [])
        finally:
            try:
                self.call("search/delete", {"id": job}, post=True)
            except (urllib.error.URLError, OSError):
                pass

    def add(self, row, save_path):
        """Submit a result; return its unique confirmation tag for page links.

        The plugin runs inside qBittorrent, with the same per-download settings
        as a magnet. Only this submission receives the returned tag, so a
        concurrent manual download cannot be mistaken for the requested one.
        """
        url = row["fileUrl"]
        params = {"urls": url}
        tag = None
        if not url.startswith("magnet:"):
            plugin = row.get("engineName", "")
            if not plugin:
                raise ValueError("Page-link search result has no search plugin name")
            if self._api_version is None:
                version = self.call("app/webapiVersion").strip()
                self._api_version = tuple(int(part) for part in version.split("."))
            if self._api_version < (2, 13, 1):
                raise ValueError("Page-link downloads require qBittorrent 5.2 or newer "
                                 "(Web API 2.13.1). Please update qBittorrent.")
            tag = "watcher-" + uuid.uuid4().hex
            params.update(downloader=plugin, tags=tag)
        if save_path:
            params.update(savepath=save_path, autoTMM="false")
        self.call("torrents/add", params, post=True)
        return tag

    def torrents(self):
        return json.loads(self.call("torrents/info"))

    def move(self, torrent_hash, folder):
        self.call(
            "torrents/setLocation",
            {"hashes": torrent_hash, "location": folder},
            post=True,
        )


def apply_download_limit(qb, limit):
    """Make qBittorrent download at most `limit` torrents at a time.

    This uses qBittorrent's own queue (Options > BitTorrent > Torrent
    Queueing): torrents over the limit show as "Queued" and start by
    themselves when a slot frees up. It applies to every download in
    qBittorrent, not only watched series. A limit of 0 switches the queue off.
    Returns True if a qBittorrent setting was changed.
    """
    prefs = json.loads(qb.call("app/preferences"))
    wanted = {}
    if limit <= 0:
        if prefs.get("queueing_enabled"):
            wanted["queueing_enabled"] = False
    else:
        if not prefs.get("queueing_enabled"):
            # Switching the queue on also activates qBittorrent's limits on
            # seeding and on total active torrents; leave those unlimited so
            # only the number of downloads changes.
            wanted.update(
                queueing_enabled=True, max_active_uploads=-1, max_active_torrents=-1
            )
        elif 0 <= prefs.get("max_active_torrents", -1) < limit:
            wanted["max_active_torrents"] = limit
        if prefs.get("max_active_downloads") != limit:
            wanted["max_active_downloads"] = limit
    if wanted:
        qb.call("app/setPreferences", {"json": json.dumps(wanted)}, post=True)
    return bool(wanted)
