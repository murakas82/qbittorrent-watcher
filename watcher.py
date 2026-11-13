"""Series watcher for qBittorrent.

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

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "series.json"
LOG_FILE = HERE / "watcher.log"
MAX_EPISODES_PER_RUN = 30  # safety cap when catching up on a season
TASK_NAME = "qBittorrent Series Watcher"


def log(message):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}"
    print(line)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


class QBittorrent:
    def __init__(self, base_url):
        self.base = base_url.rstrip("/") + "/api/v2/"

    def call(self, endpoint, params=None, post=False):
        data = urllib.parse.urlencode(params or {})
        url = self.base + endpoint
        if post:
            request = urllib.request.Request(url, data=data.encode("utf-8"))
        else:
            request = urllib.request.Request(url + ("?" + data if data else ""))
        with urllib.request.urlopen(request, timeout=30) as response:
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
            # This route ignores save_path; main() moves the torrent afterwards.
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


def name_matches(name, text):
    """True if the series name is in the text, ignoring case, punctuation
    and spacing, so "example show" and "Example-Show" both match "ExampleShow"."""
    return normalize(name).replace(" ", "") in normalize(text).replace(" ", "")


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
    name = normalize(show["name"])
    tag = episode_pattern(show)
    for torrent in qb.torrents():
        title = normalize(torrent["name"])
        found = tag.search(title)
        if found and name_matches(name, title[: found.start()]):
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
    name = normalize(show["name"])
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
        if not name_matches(name, title[: tag.start()]):
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
    nothing when releases are called "ExampleShow". If the name as typed gives no usable
    result, try once more with the name written as one word.
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


def remove_finished(qb, show, dry_run):
    """Take the show's finished episodes out of qBittorrent's list.

    Only touches episodes in the show's download history, only once they are
    100% complete, and never deletes the downloaded files.
    """
    name = normalize(show["name"])
    patterns = [
        (item["episode"], episode_pattern(dict(show, next_episode=item["episode"])))
        for item in show.get("history", [])
    ]
    for torrent in qb.torrents():
        state = torrent.get("state", "")
        # qBittorrent's names for "complete and idle or seeding". Torrents that
        # are being checked or moved are left for a later run.
        finished = torrent.get("progress", 0) >= 1 and state in (
            "uploading",
            "stalledUP",
            "pausedUP",
            "stoppedUP",
            "queuedUP",
            "forcedUP",
        )
        if not finished:
            continue
        title = normalize(torrent["name"])
        for episode, pattern in patterns:
            found = pattern.search(title)
            if found and name_matches(name, title[: found.start()]):
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
                break


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


def save_config(config):
    """Write series.json in one step, so a reader never sees half a file."""
    temp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
    temp.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(temp, CONFIG_FILE)


def same_series(a, b):
    return (
        normalize(a["name"]).replace(" ", "") == normalize(b["name"]).replace(" ", "")
        and a["season"] == b["season"]
    )


def record_progress(show, title):
    """Mark the show's next episode as downloaded, in series.json.

    Re-reads the file first, so changes made in the window while the watcher
    was running are kept. The history is what stops an episode from being
    downloaded again after its files or its torrent have been deleted.
    """
    episode = show["next_episode"]
    config = load_config()
    for entry in config["series"]:
        if same_series(entry, show):
            history = entry.setdefault("history", [])
            if not any(item["episode"] == episode for item in history):
                history.append(
                    {
                        "episode": episode,
                        "title": title or "",
                        "added": f"{datetime.now():%Y-%m-%d %H:%M}",
                    }
                )
            entry["next_episode"] = max(entry.get("next_episode", 1), episode + 1)
            save_config(config)
            return


def main():
    if "--install-schedule" in sys.argv:
        return install_schedule()
    if "--remove-schedule" in sys.argv:
        return remove_schedule()
    dry_run = "--dry-run" in sys.argv
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

    for show in config["series"]:
        if not show.get("enabled", True):
            continue
        if show.get("remove_when_done"):
            try:
                remove_finished(qb, show, dry_run)
            except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
                log(f"{show['name']}: could not remove finished torrents ({error})")
        for _ in range(MAX_EPISODES_PER_RUN):
            label = f"{show['name']} S{show['season']:02d}E{show['next_episode']:02d}"
            pattern = label
            try:
                already_there = in_client(qb, show)
                results = [] if already_there else search_episode(qb, show, timeout)
            except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
                log(f"{label}: search failed ({error})")
                break

            got_title = None
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
                    for row in candidates[:3]:
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
                    if not added:
                        break
                    got_title = added["name"]

            if not dry_run:
                # Save after every episode so a crash never causes a duplicate.
                record_progress(show, got_title)
            show["next_episode"] += 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
