"""Download orchestration shared by the CLI and desktop window."""

import os
import re
import time
import urllib.error
from datetime import datetime

from matching import (find_episode, find_movie, magnet_ids, pick_candidates,
                      pick_movie_candidates, torrent_matches_ids)
from qb_client import (SEARCH_PLUGIN_HELP, NoSearchPluginsError, QBittorrent,
                       apply_download_limit)
from store import ConfigStore, FileLock

MAX_EPISODES_PER_RUN = 30
# Exit code of a check that stopped because no search plugin is enabled, so
# the window can tell it apart from "qBittorrent unreachable" (1).
NO_PLUGINS_EXIT = 3
ADD_WAIT_SECONDS = 45
DOWNLOAD_SETTINGS = ("name", "season", "next_episode", "quality", "min_seeders",
                     "must_not_contain", "save_path", "part_of_title")
MOVIE_SETTINGS = ("name", "year", "quality", "min_seeders", "must_not_contain", "save_path")
FINISHED_STATES = ("uploading", "stalledUP", "pausedUP", "stoppedUP", "queuedUP", "forcedUP")


def in_client(qb, show):
    """Return qBittorrent's torrent for the show's next episode, or None."""
    matches = find_episode(qb.torrents(), show)
    return matches[0] if matches else None


def wait_for_added(qb, row, show, known, tag=None):
    """Wait for the torrent just submitted to appear in qBittorrent.

    Returns (torrent, ours). `ours` is True only when the torrent is certainly
    the one the watcher submitted, and only then may its ID be recorded for
    removal later. `known` holds the IDs that were in qBittorrent just before
    the add, so an existing torrent is never claimed as a new addition.

    Magnets are confirmed by their exact hash. Page links are submitted with
    a unique tag that qBittorrent carries through plugin resolution. Their
    torrent names need not equal the search result's title. Untagged torrents
    are never accepted as confirmation of a page-link submission.
    """
    expected = magnet_ids(row.get("fileUrl", ""))
    if not expected and not tag:
        return None, False
    known = {torrent_hash.lower() for torrent_hash in known}
    deadline = time.monotonic() + ADD_WAIT_SECONDS
    while True:
        torrents = qb.torrents()
        if expected:
            for torrent in torrents:
                if torrent_matches_ids(torrent, expected):
                    return torrent, torrent.get("hash", "").lower() not in known
        elif tag:
            for torrent in torrents:
                tags = {value.strip() for value in torrent.get("tags", "").split(",")}
                torrent_hash = torrent.get("hash", "").lower()
                if torrent_hash and tag in tags:
                    return torrent, torrent_hash not in known
        if time.monotonic() >= deadline:
            return None, False
        time.sleep(3)


def watcher_tag(torrent):
    """The watcher's own submission tag on a torrent, or None.

    Only the watcher creates "watcher-<random>" tags, one per submission, so a
    torrent carrying one is certainly the watcher's, even if it turned up
    after the confirmation wait or its progress could not be saved then.
    """
    for tag in str(torrent.get("tags", "")).split(","):
        tag = tag.strip()
        if tag.startswith("watcher-"):
            return tag
    return None


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


def movie_folder_name(movie):
    """'Title (Year)', without characters Windows does not allow in folder names."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", movie["name"])
    name = " ".join(name.split()).rstrip(". ")
    return f"{name} ({movie['year']})" if name else str(movie["year"])


def movie_destination(movie, config):
    """Where a movie goes, and qBittorrent's content layout for it.

    A movie with its own folder goes exactly there. Otherwise it gets a
    'Title (Year)' folder inside the movie folder, and the torrent's own top
    folder is dropped, so a single video file and a multi-file release both
    end up directly in that one folder.
    """
    if movie["save_path"]:
        return movie["save_path"], None
    base = config.get("movie_folder", "")
    if not base:
        return "", None
    return os.path.join(base, movie_folder_name(movie)), "NoSubfolder"


def search_movie(qb, movie, timeout):
    """Search for a movie by title and year, like search_episode does for series."""
    names = [movie["name"].strip()]
    joined = re.sub(r"[^A-Za-z0-9]+", "", movie["name"])
    if joined and joined.lower() != names[0].lower():
        names.append(joined)
    results = []
    for name in names:
        results = qb.search(f"{name} {movie['year']}", timeout)
        if pick_movie_candidates(results, movie):
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
        self.search_plugins_enabled = None  # latest probe: True, False, or unknown

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

    def movie_unchanged(self, movie, destination=None):
        """True if the movie is still wanted as it was when its search started.

        With `destination`, also true only if the movie would still go there,
        so a movie folder changed during the search is not ignored.
        """
        config = self.store.load()
        current = next((m for m in config["movies"] if m["id"] == movie["id"]), None)
        return (current is not None and current["enabled"] and not current["done"] and
                all(current.get(key) == movie.get(key) for key in MOVIE_SETTINGS) and
                (destination is None or movie_destination(current, config) == destination))

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
        # A storage failure says nothing about qBittorrent's availability.
        # Let the GUI report it without triggering an offline transition.
        config = self.store.load()
        self.search_plugins_enabled = None
        try:
            qb = self.client_factory(config["qbittorrent_url"])
            qb.call("app/version", timeout=3)
        except urllib.error.HTTPError:
            return True  # running but refusing us; the check reports the error
        except (urllib.error.URLError, OSError):
            return False
        try:
            self.search_plugins_enabled = qb.has_enabled_search_plugins(timeout=3)
        except (OSError, ValueError, AttributeError):
            # AttributeError: a client without the plugin probe (tests, custom
            # clients) must not stop the window's monitor thread.
            # An unavailable plugin list does not mean the client is offline
            # or that its plugins are disabled. A check reports search errors.
            pass
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
            watched = {s["id"] for s in config["series"] + config["movies"]
                       if s["enabled"] and s["remove_when_done"]}
            records = [d for d in config["downloads"] if d["managed"] and d["status"] == "added"
                       and (d.get("series_id") or d.get("movie_id")) in watched]
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
                show = (self.store.current_entry(record["series_id"]) if "series_id" in record
                        else self.store.current_movie(record["movie_id"]))
                if show is None or not show["enabled"] or not show["remove_when_done"]:
                    continue
                if "movie_id" in record:
                    record = dict(record, label=str(show["year"]))
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
        if not config["series"] and not config["movies"]:
            print("No series or movies to watch yet. Open watcher_gui.pyw and click 'Add series…'.")
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

        # Ask once per check whether any search plugin is enabled, with the
        # same patience as a search. Only a clear "none" stops the check; if
        # the list cannot be read, the searches go ahead as they always did.
        wanted_movies = [m for m in config["movies"] if m["enabled"] and not m["done"]]
        if wanted_movies or any(show.get("enabled", True) for show in config["series"]):
            try:
                plugins_enabled = qb.has_enabled_search_plugins(timeout=timeout)
            except (urllib.error.URLError, OSError, ValueError, AttributeError) as error:
                self.log(f"Could not read qBittorrent's search plugins ({error}); searching anyway")
                plugins_enabled = None
            if plugins_enabled is False:
                self.log(SEARCH_PLUGIN_HELP)
                return NO_PLUGINS_EXIT

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
                except NoSearchPluginsError as error:
                    self.log(str(error))
                    return NO_PLUGINS_EXIT
                except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
                    self.log(f"{label}: search failed ({error})")
                    break

                got_title = None
                got_hash = None
                got_managed = False
                add_tag = None
                if already_there:
                    got_title = already_there["name"]
                    got_hash = already_there.get("hash")
                    add_tag = watcher_tag(already_there)
                    got_managed = add_tag is not None
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
                        outcome, torrent, ours, add_tag = self.try_add(
                            qb, candidates, label, show.get("save_path", ""),
                            lambda torrents, show=show: find_episode(torrents, show),
                            lambda show=show: self.unchanged(show))
                        added = torrent if outcome == "added" else None
                        appeared = torrent if outcome == "appeared" else None
                        if outcome == "edited":
                            # Go round again: the top of the loop re-reads the
                            # series and stops, or carries on with the new settings.
                            self.log(f"{label}: series was changed in the window, not adding with the old settings")
                            continue
                        if appeared:
                            got_title = appeared["name"]
                            got_hash = appeared.get("hash")
                            got_managed = ours  # true only for the watcher's own tag
                            self.log(f"{label}: appeared in qBittorrent during the search, not adding")
                        elif not added:
                            break
                        else:
                            got_title = added["name"]
                            got_hash = added.get("hash")
                            got_managed = ours

                if not dry_run:
                    # Save after every episode so a crash never causes a duplicate.
                    try:
                        self.store.record_download(show, got_title, got_hash, managed=got_managed)
                    except (OSError, ValueError) as error:
                        self.log(f"{label}: could not save progress ({error})")
                        break
                    if add_tag and got_managed:
                        # The persisted hash now identifies the managed torrent;
                        # remove the temporary tag from qBittorrent's tag list.
                        try:
                            qb.call("torrents/deleteTags", {"tags": add_tag}, post=True)
                        except (urllib.error.URLError, OSError):
                            pass
                show["next_episode"] += 1

        for movie in wanted_movies:
            result = self.check_movie(qb, movie, config, timeout, dry_run)
            if result is not None:
                return result
        return 0

    def try_add(self, qb, candidates, label, folder, find_existing, still_wanted,
                content_layout=None):
        """Add the best of the candidates and confirm it really arrived.

        Returns (outcome, torrent, ours, tag). The outcome is "added", or
        "appeared" when the same item was added by hand meanwhile, "edited"
        when the entry was paused or changed in the window, or "failed".
        """
        added, ours, add_tag = None, False, None
        for row in candidates[:3]:
            # A search can take over a minute, and each attempt can wait most
            # of a minute. Before every attempt, make sure the entry was not
            # paused or edited.
            if not still_wanted():
                return "edited", None, False, None
            summary = f"{row['fileName']} ({row.get('nbSeeders', 0)} seeders)"
            try:
                # Look at what is in qBittorrent right now. If the item was
                # added by hand during the search, leave it at that. Otherwise
                # remember what is there, so only a new torrent counts as this add.
                present = qb.torrents()
                by_hand = find_existing(present)
                if by_hand:
                    tag = watcher_tag(by_hand[0])
                    return "appeared", by_hand[0], tag is not None, tag
                url = row.get("fileUrl", "")
                if url.startswith("magnet:") and not magnet_ids(url):
                    # Its torrent could never be confirmed, and an unconfirmed
                    # add would be followed by the next release: the same item
                    # downloaded more than once. Skip it instead.
                    self.log(f"{label}: skipped a magnet link without a readable torrent ID: {summary}")
                    continue
                known = {t.get("hash", "").lower() for t in present}
                add_tag = (qb.add(row, folder, content_layout) if content_layout
                           else qb.add(row, folder))
                added, ours = wait_for_added(qb, row, None, known, add_tag)
            except (urllib.error.URLError, OSError, ValueError) as error:
                self.log(f"{label}: adding failed ({error})")
            if added:
                self.log(f"{label}: added {summary}")
                if folder and ours:
                    # Ensure the confirmed torrent uses the requested folder.
                    try:
                        qb.move(added["hash"], folder)
                    except (urllib.error.URLError, OSError) as error:
                        self.log(f"{label}: could not move to {folder} ({error})")
                return "added", added, ours, add_tag
            self.log(f"{label}: did not appear in qBittorrent: {summary}")
        return "failed", None, False, add_tag

    def check_movie(self, qb, movie, config, timeout, dry_run):
        """Look for one movie and add it. Returns an exit code to stop the check, or None."""
        if not dry_run:
            movie = self.store.current_movie(movie["id"])
            if movie is None or not movie["enabled"] or movie["done"]:
                return None
        label = f"{movie['name']} ({movie['year']})"
        folder, layout = movie_destination(movie, config)
        try:
            existing = find_movie(qb.torrents(), movie)
            results = [] if existing else search_movie(qb, movie, timeout)
        except NoSearchPluginsError as error:
            self.log(str(error))
            return NO_PLUGINS_EXIT
        except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
            self.log(f"{label}: search failed ({error})")
            return None

        title, torrent_hash, managed, add_tag = None, None, False, None
        if existing:
            title, torrent_hash = existing[0]["name"], existing[0].get("hash")
            add_tag = watcher_tag(existing[0])
            managed = add_tag is not None
            self.log(f"{label}: already in qBittorrent, marking as downloaded")
        else:
            candidates = pick_movie_candidates(results, movie)
            if not candidates:
                self.log(f"{label}: not found yet ({len(results)} results, none matched)")
                return None
            if dry_run:
                best = candidates[0]
                self.log(f"{label}: would add {best['fileName']} ({best.get('nbSeeders', 0)} seeders)")
                return None
            # The search can take a minute: use the movie folder as it is now.
            folder, layout = movie_destination(movie, self.store.load())
            outcome, torrent, ours, add_tag = self.try_add(
                qb, candidates, label, folder,
                lambda torrents: find_movie(torrents, movie),
                lambda: self.movie_unchanged(movie, (folder, layout)), layout)
            if outcome == "edited":
                self.log(f"{label}: movie was changed in the window, leaving it for the next check")
                return None
            if outcome == "failed":
                return None
            title, torrent_hash = torrent["name"], torrent.get("hash")
            managed = ours
            if outcome == "appeared":
                self.log(f"{label}: appeared in qBittorrent during the search, not adding")
        if dry_run:
            return None
        try:
            self.store.record_movie(movie, title, torrent_hash, managed=managed)
        except (OSError, ValueError) as error:
            self.log(f"{label}: could not save progress ({error})")
            return None
        if add_tag and managed:
            try:
                qb.call("torrents/deleteTags", {"tags": add_tag}, post=True)
            except (urllib.error.URLError, OSError):
                pass
        return None
