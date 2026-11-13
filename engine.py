"""Download orchestration shared by the CLI and desktop window."""

import re
import time
import urllib.error
from datetime import datetime

from matching import find_episode, magnet_hash, normalize, pick_candidates
from qb_client import QBittorrent, apply_download_limit
from store import ConfigStore, FileLock

MAX_EPISODES_PER_RUN = 30
ADD_WAIT_SECONDS = 45
DOWNLOAD_SETTINGS = ("name", "season", "next_episode", "quality", "min_seeders",
                     "must_not_contain", "save_path", "part_of_title")
FINISHED_STATES = ("uploading", "stalledUP", "pausedUP", "stoppedUP", "queuedUP", "forcedUP")


def in_client(qb, show):
    """Return qBittorrent's torrent for the show's next episode, or None."""
    matches = find_episode(qb.torrents(), show)
    return matches[0] if matches else None


def wait_for_added(qb, row, show, known):
    """Wait for the torrent just submitted to appear in qBittorrent.

    Returns (torrent, ours). `ours` is True only when the torrent is certainly
    the one the watcher submitted, and only then may its ID be recorded for
    removal later. `known` holds the IDs that were in qBittorrent just before
    the add, so an existing torrent is never claimed as a new addition.

    A magnet link names its torrent's ID, so that exact torrent is looked
    for, even if another release appears first. A plugin download does not
    expose an ID: its full release title must match before progress can be
    recorded, and ownership remains unverified. Such torrents are never moved
    or removed automatically, even if only one new matching torrent appears.
    """
    expected = magnet_hash(row.get("fileUrl", ""))
    release_title = normalize(row.get("fileName", ""))
    known = {torrent_hash.lower() for torrent_hash in known}
    deadline = time.monotonic() + ADD_WAIT_SECONDS
    while True:
        torrents = qb.torrents()
        if expected:
            for torrent in torrents:
                if torrent.get("hash", "").lower() == expected:
                    return torrent, expected not in known
        elif release_title:
            for torrent in find_episode(torrents, show):
                if (torrent.get("hash", "").lower() not in known
                        and normalize(torrent["name"]) == release_title):
                    return torrent, False
        if time.monotonic() >= deadline:
            return None, False
        time.sleep(3)


def search_episode(qb, show, timeout):
    """Search for the show's next episode and return the raw results.

    Torrent sites search for the name as typed, so "example show" finds
    nothing when releases are called "ExampleShow". If the name as typed gives
    no usable result, try once more with the name written as one word.
    """
    tag = f"S{show['season']:02d}E{show['next_episode']:02d}"
    names = [show["name"].strip()]
    joined = re.sub(r"[^A-Za-z0-9]+", "", show["name"])
    if joined and joined.lower() != names[0].lower():
        names.append(joined)
    results = []
    for name in names:
        results = qb.search(f"{name} {tag}", timeout)
        if pick_candidates(results, show):
            break
    return results


class WatcherEngine:
    """Own search/add/confirm/cleanup decisions; callers only schedule work."""

    def __init__(self, store=None, client_factory=QBittorrent, logger=None,
                 max_episodes=MAX_EPISODES_PER_RUN):
        self.store = store if store is not None else ConfigStore()
        self.client_factory = client_factory
        self.logger = logger
        self.max_episodes = max_episodes
        self.log_file = self.store.path.parent / "watcher.log"

    def log(self, message):
        if self.logger is not None:
            self.logger(message)
            return
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}"
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode("ascii", "backslashreplace").decode("ascii"))
        with self.log_file.open("a", encoding="utf-8") as target:
            target.write(line + "\n")

    def check(self, dry_run=False):
        lock = FileLock(self.store.path.parent / "watcher.lock")
        if not lock.acquire():
            self.log("Another check is already running.")
            return 0
        try:
            return self._run_check(dry_run)
        finally:
            lock.release()

    def unchanged(self, show):
        current = self.store.current_entry(show["id"])
        return (current is not None and current["enabled"] and
                all(current.get(key) == show.get(key) for key in DOWNLOAD_SETTINGS))

    def apply_limit(self):
        """Apply the persisted limit; also used after a GUI setting change."""
        config = self.store.load()
        if "max_downloads" in config:
            qb = self.client_factory(config["qbittorrent_url"])
            apply_download_limit(qb, config["max_downloads"])

    def poll(self, tidy=False):
        """Probe connectivity and optionally tidy, independent of long checks."""
        try:
            config = self.store.load()
            qb = self.client_factory(config["qbittorrent_url"])
            qb.call("app/version", timeout=3)
        except urllib.error.HTTPError:
            return True  # running but refusing us; the check reports the error
        except (urllib.error.URLError, OSError, ValueError):
            return False
        if tidy:
            self.cleanup(qb=qb)
        return True

    def cleanup(self, qb=None, dry_run=False):
        """Remove only known managed torrents, across all seasons of a series."""
        lock = FileLock(self.store.path.parent / "cleanup.lock")
        if not lock.acquire():
            return
        try:
            config = self.store.load()
            watched = {s["id"]: s for s in config["series"]
                       if s["enabled"] and s["remove_when_done"]}
            records = [d for d in config["downloads"] if d["managed"]
                       and d["status"] == "added" and d["series_id"] in watched]
            if not records:
                return
            qb = qb if qb is not None else self.client_factory(config["qbittorrent_url"])
            torrents = {t.get("hash", "").lower(): t for t in qb.torrents()}
            for record in records:
                torrent = torrents.get(record["hash"].lower())
                if not torrent or torrent.get("progress", 0) < 1 or torrent.get("state") not in FINISHED_STATES:
                    continue
                # The user may have paused, removed or disabled cleanup since
                # the client request. Read the current setting before deleting.
                show = self.store.current_entry(record["series_id"])
                if show is None or not show["enabled"] or not show["remove_when_done"]:
                    continue
                tag = (f"S{record['season']:02d}E{record['episode']:02d}"
                       if record["season"] is not None and record["episode"] is not None
                       else record.get("label", ""))
                label = f"{show['name']} {tag}".rstrip()
                if dry_run:
                    self.log(f"{label}: finished, would remove from qBittorrent (files kept)")
                else:
                    qb.call("torrents/delete", {"hashes": torrent["hash"],
                                              "deleteFiles": "false"}, post=True)
                    # Persist each successful removal even if a later request
                    # fails or qBittorrent closes during this cleanup pass.
                    self.store.mark_removed({record["id"]})
                    self.log(f"{label}: finished, removed from qBittorrent (files kept)")
        except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
            self.log(f"Could not remove finished torrents ({error})")
        finally:
            lock.release()

    def _run_check(self, dry_run):
        config = self.store.load()
        if not config["series"]:
            print("No series to watch yet. Open watcher_gui.pyw and click 'Add series…'.")
            return 0
        qb = self.client_factory(config.get("qbittorrent_url", "http://127.0.0.1:8080"))
        timeout = config.get("search_timeout_seconds", 90)

        try:
            qb.call("app/version")
        except urllib.error.HTTPError as error:
            if error.code == 403:
                self.log(
                    "qBittorrent refused the connection (403). In Tools > Options > "
                    "Web UI, tick 'Bypass authentication for clients on localhost'."
                )
            else:
                self.log(f"qBittorrent answered with an error: {error}")
            return 1
        except (urllib.error.URLError, OSError) as error:
            self.log(f"Could not reach qBittorrent ({error}). Is it running with Web UI on?")
            return 1

        # Only present once the setting has been used; until then qBittorrent's
        # own queue settings are left alone.
        if "max_downloads" in config and not dry_run:
            limit = config["max_downloads"]
            try:
                if apply_download_limit(qb, limit):
                    self.log(
                        f"qBittorrent now downloads {limit} at a time"
                        if limit > 0
                        else "qBittorrent download limit switched off"
                    )
            except (urllib.error.URLError, OSError, ValueError, TypeError) as error:
                self.log(f"Could not set the download limit ({error})")

        self.cleanup(qb=qb, dry_run=dry_run)

        for show in config["series"]:
            if not show.get("enabled", True):
                continue
            for _ in range(self.max_episodes):
                # The list was read when the check started. Read this series again
                # before each episode, so pausing, removing or editing it in the
                # window takes effect straight away and not only at the next check.
                if not dry_run:
                    entry = self.store.current_entry(show["id"])
                    if entry is None or not entry.get("enabled", True):
                        self.log(f"{show['name']}: paused or removed in the window, stopping")
                        break
                    show = entry
                label = f"{show['name']} S{show['season']:02d}E{show['next_episode']:02d}"
                try:
                    already_there = in_client(qb, show)
                    results = [] if already_there else search_episode(qb, show, timeout)
                except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
                    self.log(f"{label}: search failed ({error})")
                    break

                got_title = None
                got_hash = None  # set only for torrents added here, never for found ones
                if already_there:
                    got_title = already_there["name"]
                    self.log(f"{label}: already in qBittorrent, skipping")
                else:
                    candidates = pick_candidates(results, show)
                    if not candidates:
                        self.log(f"{label}: not found yet ({len(results)} results, none matched)")
                        break

                    if dry_run:
                        best = candidates[0]
                        self.log(
                            f"{label}: would add {best['fileName']} "
                            f"({best.get('nbSeeders', 0)} seeders)"
                        )
                    else:
                        # Try the best few in turn until one really shows up.
                        added = None
                        ours = False  # is `added` certainly the torrent submitted here?
                        appeared = None  # the episode turned up without the watcher adding it
                        edited = False
                        for row in candidates[:3]:
                            # A search can take over a minute, and each attempt
                            # can wait most of a minute. Before every attempt,
                            # make sure the series was not paused or edited.
                            if not self.unchanged(show):
                                edited = True
                                break
                            summary = f"{row['fileName']} ({row.get('nbSeeders', 0)} seeders)"
                            try:
                                # Look at what is in qBittorrent right now. If the
                                # episode was added by hand during the search, leave
                                # it at that. Otherwise remember what is there, so
                                # only a torrent that is new can count as this add.
                                present = qb.torrents()
                                by_hand = find_episode(present, show)
                                if by_hand:
                                    appeared = by_hand[0]
                                    break
                                known = {t.get("hash", "").lower() for t in present}
                                qb.add(row, show.get("save_path", ""))
                                added, ours = wait_for_added(qb, row, show, known)
                            except (urllib.error.URLError, OSError, ValueError) as error:
                                self.log(f"{label}: adding failed ({error})")
                            if added:
                                self.log(f"{label}: added {summary}")
                                folder = show.get("save_path", "")
                                if folder and ours:
                                    # Set the folder after the add too, because
                                    # plugin downloads ignore it at add time.
                                    try:
                                        qb.move(added["hash"], folder)
                                    except (urllib.error.URLError, OSError) as error:
                                        self.log(f"{label}: could not move to {folder} ({error})")
                                break
                            self.log(f"{label}: did not appear in qBittorrent: {summary}")
                        if edited:
                            # Go round again: the top of the loop re-reads the
                            # series and stops, or carries on with the new settings.
                            self.log(f"{label}: series was changed in the window, not adding with the old settings")
                            continue
                        if appeared:
                            got_title = appeared["name"]
                            self.log(f"{label}: appeared in qBittorrent during the search, not adding")
                        elif not added:
                            break
                        else:
                            got_title = added["name"]
                            # Record the ID only when the torrent is certainly the
                            # watcher's own; only recorded torrents are ever removed.
                            got_hash = added.get("hash") if ours else None

                if not dry_run:
                    # Save after every episode so a crash never causes a duplicate.
                    try:
                        self.store.record_download(show, got_title, got_hash)
                    except (OSError, ValueError) as error:
                        self.log(f"{label}: could not save progress ({error})")
                        break
                show["next_episode"] += 1

        return 0
