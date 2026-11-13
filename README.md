# qBittorrent Watcher

A small tool that watches for new episodes of the series you follow and adds
them to qBittorrent automatically.

It searches through the search plugins you already have in qBittorrent's
Search tab, so there are no indexers, RSS feeds or extra services to set up.
It is two Python files with no dependencies beyond Python itself.

## What it does

- Keeps a list of series, each with a season and the next episode to get.
- Opens together with qBittorrent, checks straight away and then every hour,
  searching for that episode through qBittorrent's search plugins.
- Picks the release with the most seeders that matches your quality, and adds it.
- Moves on to the following episode, and catches up on several at once if needed.
- Remembers every episode it has downloaded, so deleting the files or the
  torrent never causes a second download.
- Optionally removes finished torrents from qBittorrent (the files are kept).
- Can limit how many downloads run at a time; the rest wait in a queue.
- Has a window for managing the list, so you never have to edit a file.

## Requirements

- Windows 10 or 11. The watcher itself is plain Python, but starting with
  Windows and the window have only been used on Windows.
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

That is all. From the first time you open the window, the watcher looks after
itself:

- It starts with Windows and waits out of sight.
- When qBittorrent opens, the window appears and a check runs straight away.
- While qBittorrent is running, it checks again every hour.
- When qBittorrent closes, the window hides again.

Closing the window with the X only hides it. To switch all of this off, untick
*Start with Windows and open with qBittorrent* at the bottom of the window;
after that the X closes the program, and checks only happen while you have the
window open.

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

### Downloads at a time

The box at the top right sets how many torrents qBittorrent downloads at once.
With it set to 2, two episodes download and the rest wait as *Queued* in
qBittorrent; each time one finishes, the next starts by itself.

This switches on qBittorrent's own queue (Options > BitTorrent > Torrent
Queueing), so it applies to every download in qBittorrent, not only watched
series. Seeding is left unlimited. Set the box to 0 to switch the queue off.
As long as you never touch the box, the watcher leaves qBittorrent's queue
settings alone.

### Series settings

- **Series name** – the title as it appears in torrent names. Capitals, spaces
  and punctuation do not matter (`example show` finds `ExampleShow`), but real
  spelling mistakes find nothing. The release title has to be this name and
  nothing more, apart from a leading "The" and a year or country tag after it
  (`Example Show 2024`, `Example Show US`). So `You` does not pick up
  `Young Sheldon`.
- **Name is only part of a longer title** – tick this if you typed a short
  form and releases use a longer title that contains it, for example a
  subtitle on its own. The name then matches as a run of whole words anywhere
  in the title. This is looser, so prefer the full title when you can.
- **Next episode to get** – earlier episodes are never downloaded. Lower this
  number if you want an old episode again.
- **Quality** – a word that must be in the release name, such as `1080p`.
- **Minimum seeders** – releases with fewer seeders are ignored.
- **Skip releases with** – words that rule a release out, such as `CAM`.
- **Download folder** – leave empty to use qBittorrent's default folder.
- **Remove from qBittorrent when finished** – takes completed episodes out of
  qBittorrent's list within about half a minute of finishing, as long as the
  watcher is running. The files stay; seeding stops. Only torrents the watcher
  added itself are removed, recognised by their torrent ID; a torrent you
  added by hand is never touched, whatever it is called. If you change a
  series to another season while episodes are still downloading, those are
  still removed when they finish.

## Running it without the window

```
python watcher.py            search and add new episodes
python watcher.py --dry-run  show what would be added, add nothing
```

If you would rather not keep the window program running, untick *Start with
Windows and open with qBittorrent* and let Windows Task Scheduler run the
watcher every hour instead:

```
python watcher.py --install-schedule
python watcher.py --remove-schedule
```

## How it decides

For each series the watcher searches for `Name SxxEyy`. A result is accepted
when the title before the episode number is the series name, the quality word
is present, none of the skip words are present, and it has enough
seeders. Among those it takes the one with the most seeders, then checks that
the torrent really appeared in qBittorrent before counting the episode as done.

It works through episodes in order. If an episode cannot be found, it waits
for that episode and does not skip ahead. Pausing, removing or editing a
series in the window takes effect straight away, even in the middle of a check. Season packs are ignored, and moving
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
