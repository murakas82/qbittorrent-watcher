# qBittorrent Watcher

A small tool that watches for new episodes of the series you follow, and for
movies you are waiting for, and adds them to qBittorrent automatically.

It searches through the search plugins you already have in qBittorrent's
Search tab, so there are no indexers, RSS feeds or extra services to set up.
It uses Python's standard library, with no extra dependencies to install.

## What it does

- Keeps a list of series, each with a season and the next episode to get.
- Keeps a list of movies, each with its title and year, and downloads each one
  once a release appears, then marks it as downloaded.
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
- qBittorrent 5.2 or newer with at least one search plugin installed and
  working in the Search tab.

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
| Add series… / Add movie… | Adds a series or a movie, depending on the tab in front. |
| Edit… | Changes the selected series or movie (or double-click it). |
| Pause / Resume | Stops or restarts watching without removing it. |
| Remove | Stops watching and forgets the history. Files and torrents are not touched. |
| Check now | Runs a search immediately instead of waiting for the hourly run. |

The *Series* and *Movies* tabs hold the two lists; the buttons act on the tab
in front. The *Downloaded* tab lists what the watcher has got for the selected
series across all seasons, or for the selected movie, including replacement downloads. The table's
*Downloaded* column shows episodes from the current season. The *Activity* tab
shows the log.
Dates in both tabs use `DD.MM.YYYY`, followed by the recorded time when present.

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
  *4K (2160p)* accepts releases named `2160p`, `4K` or `UHD`.
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

Page-link downloads use qBittorrent's plugin downloader with the selected
folder and a temporary unique tag. The watcher confirms that tag, records the
torrent hash, and then removes the tag. This also works when the torrent's name
differs from the search result. Only confirmed additions are moved or removed;
downloads added by hand can appear in history without becoming managed.
This requires Web API 2.13.1 or newer (included in qBittorrent 5.2 and later).
Older clients still support magnet results and report an upgrade message for
page-link results before submitting them.

### Movies

The *Movies* tab works like the series list, with these differences:

- **Title and year** – both are required. A release is accepted only when its
  name is the title followed by the year, so `Dune 2021` never picks up the
  1984 film or `Dune Part Two`.
- **Movie folder** – the box at the top of the tab sets where movies go. Each
  movie gets its own folder in it, named `Title (Year)`, for example
  `Movies\Dune (2021)`. The torrent's files go straight into that folder,
  whether it is a single video file or a release with several files. A movie
  can have its own folder instead, set in *Edit…*; it is then used exactly as
  given.
- **Stricter defaults** – new movies are often leaked first as cinema
  recordings, so a new movie starts with a longer skip list (`CAM`, `TS`,
  `TELESYNC`, `SCREENER` and similar), at least 5 seeders and 1080p.
- **Done when downloaded** – once a movie is added, its status becomes
  *Downloaded* and it is not searched for again. If it is already in
  qBittorrent, it is marked as downloaded without adding a copy. To look for
  it again, untick *Downloaded* in *Edit…*.
- **Remove when finished** – on by default, as for new series. Only torrents
  the watcher added itself are removed, and the files are kept.

## Running it without the window

```
python watcher.py            search and add new episodes
python watcher.py --dry-run  show what would be added, add nothing
```

Use `--config path/to/series.json` to select another watch list, for example
when testing. By default both entry points use `series.json` beside the program.

If you would rather not keep the window program running, untick *Start with
Windows and open with qBittorrent* and let Windows Task Scheduler run the
watcher every hour instead:

```
python watcher.py --install-schedule
python watcher.py --remove-schedule
```

Include `--config` when installing a task for another watch list. The task
stores its absolute path, including when you supply a relative path:

```
python watcher.py --config "watch lists/series.json" --install-schedule
```

## How it decides

For each series the watcher searches for `Name SxxEyy`. A result is accepted
when the title before the episode number is the series name, the quality word
is present, none of the skip words are present, and it has enough
seeders. Among those it takes the one with the most seeders, then checks that
the torrent really appeared in qBittorrent before counting the episode as done.

It works through episodes in order. If an episode cannot be found, it waits
for that episode and does not skip ahead. Pausing, removing or editing a
series in the window takes effect during a check, before the next addition.
An addition already submitted to qBittorrent is still recorded. Season packs
are ignored, and moving to the next season is something you do yourself in
*Edit…*.

For each movie that is not yet downloaded, the watcher searches for
`Title Year` and applies the same rules, with the title-and-year match in
place of the episode number. Movies are checked after the series.

Watch entries have stable IDs. Download records keep their own season,
episode and torrent identity, so renaming a series or changing its season
preserves history and pending cleanup. Removing a watch entry forgets its
records, as described in the window's confirmation message.

## Upgrading an existing watch list

Close any old watcher window and let an ongoing check finish before replacing
the program files. The first run upgrades `series.json` to the current schema
version (3, which added movies) under the storage lock. It first saves an
exact copy of the old file as `series.json.v1.bak` or `series.json.v2.bak`,
named after the old version (using a unique suffix if that backup already
exists).
Settings, history and pending cleanup records are carried over automatically.
Keep the backup if you need to return to an older version of the program.

## Files

| File | Purpose |
|---|---|
| `watcher.py` | CLI and Windows scheduled-task entry point. Runs once and exits. |
| `watcher_gui.pyw` | The window. |
| `engine.py` | Search, add, confirmation and cleanup orchestration. |
| `qb_client.py` | qBittorrent Web API requests and preferences. |
| `matching.py` | Release-name, episode and torrent-identity matching. |
| `store.py` | Validated storage, transactions and schema migration. |
| `series.example.json` | Shows the layout of the watch list. |
| `series.json` | Your watch settings and independent download records. Created on first use, not shared. |
| `watcher.log` | What the watcher did. Created on first run. |

## Development

Run the regression and migration tests with:

```
python -m unittest discover -s tests -v
```

Tests use temporary watch lists and simulated clients. They do not contact
qBittorrent or change your watch list, downloads or Windows startup settings.
The GUI and CLI share `WatcherEngine`; only `ConfigStore` writes settings and
download records. The GUI schedules checks in a subprocess so long searches
stay independent of the window, and schedules cleanup separately every
30 seconds through the same engine.

## Troubleshooting

- **"No search plugins are enabled in qBittorrent"** – in qBittorrent, choose
  **View > Search Engine**, open the **Search** tab, and click **Search
  plugins…**. Enable at least one installed plugin. If the list is empty, click
  **Install a new one** and follow the [qBittorrent installation
  guide](https://github.com/qbittorrent/search-plugins/wiki/Install-search-plugins).
  The watcher shows a **How to fix…** button and resumes searches automatically
  when a plugin is enabled. Completed downloads are still cleaned up while
  searches are waiting.
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
