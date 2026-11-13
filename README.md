# qBittorrent Series Watcher

A small tool that watches for new episodes of the series you follow and adds
them to qBittorrent automatically.

It searches through the search plugins you already have in qBittorrent's
Search tab, so there are no indexers, RSS feeds or extra services to set up.
It is two Python files with no dependencies beyond Python itself.

## What it does

- Keeps a list of series, each with a season and the next episode to get.
- Every hour, searches for that episode through qBittorrent's search plugins.
- Picks the release with the most seeders that matches your quality, and adds it.
- Moves on to the following episode, and catches up on several at once if needed.
- Remembers every episode it has downloaded, so deleting the files or the
  torrent never causes a second download.
- Optionally removes finished torrents from qBittorrent (the files are kept).
- Has a window for managing the list, so you never have to edit a file.

## Requirements

- Windows 10 or 11. The watcher itself is plain Python, but the hourly
  schedule and the window have only been used on Windows.
- [Python 3.9 or newer](https://www.python.org/downloads/), installed with the
  default options (this includes the window toolkit).
- qBittorrent 5.x with at least one search plugin installed and working in the
  Search tab.

## Setup

1. **Get the files.** Download this repository (Code > Download ZIP) and
   unpack it somewhere permanent, for example `C:\Tools\SeriesWatcher`.

2. **Let the watcher talk to qBittorrent.** In qBittorrent, open
   Tools > Options > Web UI and tick:
   - *Web User Interface (Remote control)*
   - *Bypass authentication for clients on localhost*

   The watcher expects the default port, 8080. If you use another one, change
   `qbittorrent_url` in `series.json` after the first start.

3. **Open the window.** Double-click `watcher_gui.pyw`, then click
   *Add series…* and fill in the series name, season and the next episode you
   need.

4. **Turn on the hourly check.** Open a terminal in the folder and run:

   ```
   python watcher.py --install-schedule
   ```

   To turn it off again:

   ```
   python watcher.py --remove-schedule
   ```

qBittorrent has to be running for a check to do anything.

## Using the window

| Button | What it does |
|---|---|
| Add series… | Adds a series to watch. |
| Edit… | Changes the selected series (or double-click it). |
| Pause / Resume | Stops or restarts watching without removing the series. |
| Remove | Stops watching and forgets the history. Files and torrents are not touched. |
| Check now | Runs a search immediately instead of waiting for the hourly run. |

The *Downloaded episodes* tab lists what the watcher has got for the selected
series. The *Activity* tab shows the log.

### Series settings

- **Series name** – as it appears in torrent names. Capitals, spaces and
  punctuation do not matter (`example show` finds `ExampleShow`), but real spelling
  mistakes find nothing.
- **Next episode to get** – earlier episodes are never downloaded. Lower this
  number if you want an old episode again.
- **Quality** – a word that must be in the release name, such as `1080p`.
- **Minimum seeders** – releases with fewer seeders are ignored.
- **Skip releases with** – words that rule a release out, such as `CAM`.
- **Download folder** – leave empty to use qBittorrent's default folder.
- **Remove from qBittorrent when finished** – takes completed episodes out of
  qBittorrent's list at the next check. The files stay; seeding stops.

## Running it by hand

```
python watcher.py            search and add new episodes
python watcher.py --dry-run  show what would be added, add nothing
```

## How it decides

For each series the watcher searches for `Name SxxEyy`. A result is accepted
when the series name comes before the episode number in the release name, the
quality word is present, none of the skip words are present, and it has enough
seeders. Among those it takes the one with the most seeders, then checks that
the torrent really appeared in qBittorrent before counting the episode as done.

It works through episodes in order. If an episode cannot be found, it waits
for that episode and does not skip ahead. Season packs are ignored, and moving
to the next season is something you do yourself in *Edit…*.

## Files

| File | Purpose |
|---|---|
| `watcher.py` | The watcher. Runs once and exits. |
| `watcher_gui.pyw` | The window. |
| `series.example.json` | Shows the layout of the watch list. |
| `series.json` | Your watch list and history. Created on first use, not shared. |
| `watcher.log` | What the watcher did. Created on first run. |

## Troubleshooting

- **"qBittorrent refused the connection (403)"** – tick *Bypass authentication
  for clients on localhost* in the Web UI options.
- **"Could not reach qBittorrent"** – qBittorrent is not running, the Web UI is
  off, or it uses a port other than 8080.
- **"not found yet (0 results…)"** for an episode you can find by hand – check
  the spelling of the series name, and that your search plugins still work in
  the Search tab.

## License

MIT. See [LICENSE](LICENSE).

## A note on use

This tool only automates searches you could do by hand in qBittorrent. What
you download with it is your responsibility; use it only for content you have
the right to download.
