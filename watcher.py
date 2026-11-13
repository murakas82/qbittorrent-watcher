"""qBittorrent Watcher: downloads new episodes of the series you follow.

Reads series.json, searches for the next episode of each series through
qBittorrent's own search plugins (Web API), adds the best match, and then
moves on to the following episode.

Usage:
    python watcher.py            search and add new episodes
    python watcher.py --dry-run  search and show what would be added, add nothing
    python watcher.py --install-schedule   run the watcher every hour (Windows)
    python watcher.py --remove-schedule    stop the hourly runs
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    import msvcrt  # Windows
except ImportError:
    msvcrt = None
    import fcntl

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "series.json"
LOG_FILE = HERE / "watcher.log"
LOCK_FILE = HERE / "watcher.lock"  # held for the whole of a check
CONFIG_LOCK_FILE = HERE / "series.lock"  # held while series.json is changed
MAX_EPISODES_PER_RUN = 30  # safety cap when catching up on a season
TASK_NAME = "qBittorrent Watcher"


def log(message):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}"
    print(line)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


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
        deadline = time.time() + timeout
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
                if time.time() >= deadline:
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


class QBittorrent:
    def __init__(self, base_url):
        self.base = base_url.rstrip("/") + "/api/v2/"

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
        """Hand a search result to qBittorrent. Does not confirm it arrived."""
        url = row["fileUrl"]
        if url.startswith("magnet:"):
            params = {"urls": url}
            if save_path:
                params["savepath"] = save_path
            self.call("torrents/add", params, post=True)
        else:
            # Many plugins return a page link that only the plugin itself can
            # turn into a torrent, so let the plugin fetch it (as the GUI does).
            # This route ignores save_path; run_check moves the torrent afterwards.
            self.call(
                "search/downloadTorrent",
                {"torrentUrl": url, "pluginName": row.get("engineName", "")},
                post=True,
            )

    def torrents(self):
        return json.loads(self.call("torrents/info"))

    def move(self, torrent_hash, folder):
        self.call(
            "torrents/setLocation",
            {"hashes": torrent_hash, "location": folder},
            post=True,
        )


def normalize(text):
    """Lowercase and turn every run of punctuation into one space."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


# Words that may follow a series title in a release name without making it a
# different series: a year, or a country tag.
COUNTRY_TAGS = {"us", "uk", "au", "ca", "nz"}


def is_title_extra(word):
    return word in COUNTRY_TAGS or re.fullmatch(r"(19|20)\d\d", word) is not None


def name_matches(show, text):
    """True if `text` (a release title, up to the episode number) is this series.

    Matching is by whole words, ignoring case, punctuation and how the words
    are spaced, so "example show" and "Example-Show" both match "ExampleShow",
    but "You" does not match "Young Sheldon".

    Normally the title has to be the series name and nothing else, apart from
    a leading "The" and a trailing year or country tag ("Lupin 2021",
    "The Office US"). A series marked "part_of_title" matches when its name is
    a run of whole words anywhere in the title, for names typed as the short
    form of a longer title.
    """
    wanted = normalize(show["name"]).split()
    words = normalize(text).split()
    if not wanted or not words:
        return False
    target = "".join(wanted)

    if show.get("part_of_title"):
        for start in range(len(words)):
            joined = ""
            for word in words[start:]:
                joined += word
                if joined == target:
                    return True
                if len(joined) >= len(target):
                    break
        return False

    targets = {target}
    if wanted[0] == "the" and len(wanted) > 1:
        targets.add("".join(wanted[1:]))
    variants = [words]
    if words[0] == "the" and len(words) > 1:
        variants.append(words[1:])
    for variant in variants:
        trimmed = list(variant)
        while True:
            if "".join(trimmed) in targets:
                return True
            if len(trimmed) > 1 and is_title_extra(trimmed[-1]):
                trimmed.pop()
            else:
                break
    return False


def episode_pattern(show):
    """Regex for the show's next episode in a normalized title.

    Matches "s02e06", "s02 e06" and "2x06", but not "s02e060" or "12x06".
    """
    season, episode = show["season"], show["next_episode"]
    return re.compile(
        rf"(?<![a-z0-9])(?:s0*{season} ?e0*{episode}|0*{season}x0*{episode})(?![0-9])"
    )


def in_client(qb, show):
    """Return qBittorrent's torrent for the show's next episode, or None."""
    tag = episode_pattern(show)
    for torrent in qb.torrents():
        title = normalize(torrent["name"])
        found = tag.search(title)
        if found and name_matches(show, title[: found.start()]):
            return torrent
    return None


def wait_until_in_client(qb, show, seconds=45):
    deadline = time.time() + seconds
    while time.time() < deadline:
        torrent = in_client(qb, show)
        if torrent:
            return torrent
        time.sleep(3)
    return None


def pick_candidates(results, show):
    """Return matching results for the show's next episode, best first."""
    quality = normalize(show.get("quality", ""))
    banned = {normalize(w) for w in show.get("must_not_contain", [])}
    episode_tag = episode_pattern(show)

    candidates = []
    for row in results:
        title = normalize(row.get("fileName", ""))
        url = row.get("fileUrl", "")
        tag = episode_tag.search(title)
        if not tag or not url.startswith(("magnet:", "http://", "https://")):
            continue
        # The show name must come before the episode tag.
        if not name_matches(show, title[: tag.start()]):
            continue
        if quality and quality not in title.split():
            continue
        if banned & set(title.split()):
            continue
        if row.get("nbSeeders", 0) < show.get("min_seeders", 1):
            continue
        candidates.append(row)

    return sorted(candidates, key=lambda r: r.get("nbSeeders", 0), reverse=True)


def pick_best(results, show):
    """Return the best result for the show's next episode, or None."""
    candidates = pick_candidates(results, show)
    return candidates[0] if candidates else None


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


FINISHED_STATES = (
    # qBittorrent's names for "complete and idle or seeding". Torrents that
    # are being checked or moved are left for a later pass.
    "uploading",
    "stalledUP",
    "pausedUP",
    "stoppedUP",
    "queuedUP",
    "forcedUP",
)


def remove_finished(qb, show, dry_run):
    """Take the show's finished episodes out of qBittorrent's list.

    Only removes torrents the watcher added itself, recognised by the torrent
    ID stored in the download history, never by name. So a torrent you added
    by hand is never removed, whatever it is called. Only removes torrents
    that are 100% complete, and never deletes the downloaded files.
    """
    ours = {
        item["hash"].lower(): item["episode"]
        for item in show.get("history", [])
        if item.get("hash")
    }
    if not ours:
        return
    for torrent in qb.torrents():
        episode = ours.get(torrent.get("hash", "").lower())
        if episode is None:
            continue
        if torrent.get("progress", 0) < 1 or torrent.get("state") not in FINISHED_STATES:
            continue
        label = f"{show['name']} S{show['season']:02d}E{episode:02d}"
        if dry_run:
            log(f"{label}: finished, would remove from qBittorrent (files kept)")
        else:
            qb.call(
                "torrents/delete",
                {"hashes": torrent["hash"], "deleteFiles": "false"},
                post=True,
            )
            log(f"{label}: finished, removed from qBittorrent (files kept)")


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


def install_schedule(task_name=TASK_NAME):
    """Create a Windows scheduled task that runs the watcher every hour."""
    # pythonw.exe runs without opening a console window.
    python = Path(sys.executable).with_name("pythonw.exe")
    if not python.exists():
        python = Path(sys.executable)
    command = f'"{python}" "{Path(__file__).resolve()}"'
    done = subprocess.run(
        ["schtasks", "/Create", "/TN", task_name, "/TR", command, "/SC", "HOURLY", "/F"],
        capture_output=True,
        text=True,
    )
    print((done.stdout or done.stderr).strip())
    return done.returncode


def remove_schedule(task_name=TASK_NAME):
    done = subprocess.run(
        ["schtasks", "/Delete", "/TN", task_name, "/F"], capture_output=True, text=True
    )
    print((done.stdout or done.stderr).strip())
    return done.returncode


# ---- series.json -----------------------------------------------------------


def load_config():
    """Read series.json; a missing file means an empty list with defaults."""
    config = {
        "qbittorrent_url": "http://127.0.0.1:8080",
        "search_timeout_seconds": 90,
        "series": [],
    }
    if CONFIG_FILE.exists():
        config.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    return config


def save_config(config):
    """Write series.json in one step, so a reader never sees half a file.

    Call this only through update_config, which holds the lock.
    """
    # The process ID keeps two programs from ever sharing a temporary file.
    temp = CONFIG_FILE.with_name(f"{CONFIG_FILE.name}.{os.getpid()}.tmp")
    temp.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    for attempt in range(20):
        try:
            os.replace(temp, CONFIG_FILE)
            return
        except PermissionError:
            # Windows refuses while another program is reading the file.
            if attempt == 19:
                temp.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


def update_config(change):
    """Read series.json, apply change(config) and save, as one locked step.

    Every change to the file goes through here, from the watcher and from the
    window alike. The lock makes sure neither can save over something the
    other wrote in between. Returns the saved config.
    """
    lock = FileLock(CONFIG_LOCK_FILE)
    if not lock.acquire(timeout=15):
        raise OSError("series.json is in use by another program")
    try:
        config = load_config()
        change(config)
        save_config(config)
        return config
    finally:
        lock.release()


def same_series(a, b):
    return (
        normalize(a["name"]).replace(" ", "") == normalize(b["name"]).replace(" ", "")
        and a["season"] == b["season"]
    )


# Everything about a series that decides which release is added and where.
DOWNLOAD_SETTINGS = (
    "next_episode",
    "quality",
    "min_seeders",
    "must_not_contain",
    "save_path",
    "part_of_title",
)


def current_entry(show):
    """This series as series.json has it right now, or None if it is gone."""
    for entry in load_config()["series"]:
        if same_series(entry, show):
            return entry
    return None


def unchanged(show):
    """True if the series is still watched with the settings this check is using.

    False once it has been paused, removed or renamed in the window, or its
    next episode or any download setting has been edited.
    """
    entry = current_entry(show)
    return (
        entry is not None
        and entry.get("enabled", True)
        and all(entry.get(key) == show.get(key) for key in DOWNLOAD_SETTINGS)
    )


def record_progress(show, title, torrent_hash=None):
    """Mark the show's next episode as downloaded, in series.json.

    `torrent_hash` is given only for torrents the watcher added itself; it is
    what later allows that torrent, and no other, to be removed when finished.
    """
    episode = show["next_episode"]

    def change(config):
        for entry in config["series"]:
            if not same_series(entry, show):
                continue
            history = entry.setdefault("history", [])
            fresh = {
                "episode": episode,
                "title": title or "",
                "added": f"{datetime.now():%Y-%m-%d %H:%M}",
            }
            if torrent_hash:
                fresh["hash"] = torrent_hash
            existing = next((item for item in history if item["episode"] == episode), None)
            if existing is None:
                history.append(fresh)
            elif torrent_hash:
                # The episode was downloaded again. Keep the new torrent's ID,
                # or it could never be removed when it finishes.
                existing.update(fresh)
            # Move on only if the series still points at this episode. If the
            # next episode was edited in the window meanwhile (set back to get
            # old episodes again, say), that choice stands.
            if entry.get("next_episode") == episode:
                entry["next_episode"] = episode + 1
            return

    update_config(change)


# ---- the check -------------------------------------------------------------


def main():
    if "--install-schedule" in sys.argv:
        return install_schedule()
    if "--remove-schedule" in sys.argv:
        return remove_schedule()
    dry_run = "--dry-run" in sys.argv
    # One check at a time, so two runs can never add the same episode.
    lock = FileLock(LOCK_FILE)
    if not lock.acquire():
        print("Another check is already running.")
        return 0
    try:
        return run_check(dry_run)
    finally:
        lock.release()


def run_check(dry_run):
    config = load_config()
    if not config["series"]:
        print("No series to watch yet. Open watcher_gui.pyw and click 'Add series…'.")
        return 0
    qb = QBittorrent(config.get("qbittorrent_url", "http://127.0.0.1:8080"))
    timeout = config.get("search_timeout_seconds", 90)

    try:
        qb.call("app/version")
    except urllib.error.HTTPError as error:
        if error.code == 403:
            log(
                "qBittorrent refused the connection (403). In Tools > Options > "
                "Web UI, tick 'Bypass authentication for clients on localhost'."
            )
        else:
            log(f"qBittorrent answered with an error: {error}")
        return 1
    except (urllib.error.URLError, OSError) as error:
        log(f"Could not reach qBittorrent ({error}). Is it running with Web UI on?")
        return 1

    # Only present once the setting has been used; until then qBittorrent's
    # own queue settings are left alone.
    if "max_downloads" in config and not dry_run:
        limit = config["max_downloads"]
        try:
            if apply_download_limit(qb, limit):
                log(
                    f"qBittorrent now downloads {limit} at a time"
                    if limit > 0
                    else "qBittorrent download limit switched off"
                )
        except (urllib.error.URLError, OSError, ValueError, TypeError) as error:
            log(f"Could not set the download limit ({error})")

    for show in config["series"]:
        if not show.get("enabled", True):
            continue
        if show.get("remove_when_done"):
            try:
                remove_finished(qb, show, dry_run)
            except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
                log(f"{show['name']}: could not remove finished torrents ({error})")
        for _ in range(MAX_EPISODES_PER_RUN):
            # The list was read when the check started. Read this series again
            # before each episode, so pausing, removing or editing it in the
            # window takes effect straight away and not only at the next check.
            if not dry_run:
                entry = current_entry(show)
                if entry is None or not entry.get("enabled", True):
                    log(f"{show['name']}: paused or removed in the window, stopping")
                    break
                show = entry
            label = f"{show['name']} S{show['season']:02d}E{show['next_episode']:02d}"
            try:
                already_there = in_client(qb, show)
                results = [] if already_there else search_episode(qb, show, timeout)
            except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
                log(f"{label}: search failed ({error})")
                break

            got_title = None
            got_hash = None  # set only for torrents added here, never for found ones
            if already_there:
                got_title = already_there["name"]
                log(f"{label}: already in qBittorrent, skipping")
            else:
                candidates = pick_candidates(results, show)
                if not candidates:
                    log(f"{label}: not found yet ({len(results)} results, none matched)")
                    break

                if dry_run:
                    best = candidates[0]
                    log(
                        f"{label}: would add {best['fileName']} "
                        f"({best.get('nbSeeders', 0)} seeders)"
                    )
                else:
                    # Try the best few in turn until one really shows up.
                    added = None
                    edited = False
                    for row in candidates[:3]:
                        # A search can take over a minute, and each attempt
                        # can wait most of a minute. Before every attempt,
                        # make sure the series was not paused or edited.
                        if not unchanged(show):
                            edited = True
                            break
                        summary = f"{row['fileName']} ({row.get('nbSeeders', 0)} seeders)"
                        try:
                            qb.add(row, show.get("save_path", ""))
                            added = wait_until_in_client(qb, show)
                        except (urllib.error.URLError, OSError, ValueError) as error:
                            log(f"{label}: adding failed ({error})")
                        if added:
                            log(f"{label}: added {summary}")
                            folder = show.get("save_path", "")
                            if folder:
                                # Set the folder after the add too, because
                                # plugin downloads ignore it at add time.
                                try:
                                    qb.move(added["hash"], folder)
                                except (urllib.error.URLError, OSError) as error:
                                    log(f"{label}: could not move to {folder} ({error})")
                            break
                        log(f"{label}: did not appear in qBittorrent: {summary}")
                    if edited:
                        # Go round again: the top of the loop re-reads the
                        # series and stops, or carries on with the new settings.
                        log(f"{label}: series was changed in the window, not adding with the old settings")
                        continue
                    if not added:
                        break
                    got_title = added["name"]
                    got_hash = added.get("hash")

            if not dry_run:
                # Save after every episode so a crash never causes a duplicate.
                try:
                    record_progress(show, got_title, got_hash)
                except (OSError, ValueError) as error:
                    log(f"{label}: could not save progress ({error})")
                    break
            show["next_episode"] += 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
