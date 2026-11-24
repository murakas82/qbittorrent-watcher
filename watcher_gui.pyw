"""qBittorrent Watcher: the window for managing the watch list.

Shows the series in series.json, lets you add, edit, pause and remove them,
lists the episodes already downloaded, and can start a check right away.
Double-click this file to open it.

The window also does the automatic checking: it notices when qBittorrent
opens, checks straight away, and checks again every hour while qBittorrent
is running. With "Start with Windows" ticked it starts hidden at sign-in
(--background) and closing the window only hides it.
"""

import ctypes
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    import winreg  # Windows only; used for "start with Windows"
except ImportError:
    winreg = None

from engine import NO_PLUGINS_EXIT, WatcherEngine, movie_destination
from qb_client import NO_SEARCH_PLUGINS, SEARCH_PLUGIN_HELP
from store import MOVIE_DEFAULTS, ConfigStore, HERE

CHECK_EVERY = 3600  # seconds between automatic checks while qBittorrent runs
POLL_EVERY = 5  # seconds between looks for qBittorrent
TIDY_EVERY = 30  # seconds between removals of finished torrents
INSTANCE_PORT = 47821  # local port used to keep a single window running
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "qBittorrent Watcher"
OLD_RUN_NAME = "qBittorrent Series Watcher"  # the program's earlier name
# (shown in the window, stored in series.json). 4K is stored as "2160p" and
# also matches release names that say "4K" or "UHD".
QUALITY_CHOICES = [("Any", ""), ("4K (2160p)", "2160p"), ("1080p", "1080p"),
                   ("720p", "720p"), ("480p", "480p")]


def quality_label(value):
    return next((label for label, stored in QUALITY_CHOICES if stored == (value or "")), value)


def quality_value(label):
    return next((stored for shown, stored in QUALITY_CHOICES if shown == label), label)
DEFAULT_SKIP = "CAM, HDCAM, TELESYNC, HDTS"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def episode_ranges(episodes):
    """[1, 2, 3, 5] -> 'E01–E03, E05'."""
    parts, run = [], []
    for number in sorted(set(episodes)) + [None]:
        if run and number == run[-1] + 1:
            run.append(number)
            continue
        if run:
            first, last = run[0], run[-1]
            parts.append(f"E{first:02d}" if first == last else f"E{first:02d}–E{last:02d}")
        run = [number]
    return ", ".join(parts)


def series_key(show):
    return show["id"]


def display_date(value):
    """Format the stored ISO date for display, preserving its time or log text."""
    try:
        day = datetime.strptime(value[:10], "%Y-%m-%d")
    except ValueError:
        return value
    return day.strftime("%d.%m.%Y") + value[10:]


class SeriesDialog(tk.Toplevel):
    """Form for adding or editing one series. Result is in self.result."""

    def __init__(self, parent, show=None):
        super().__init__(parent)
        self.title("Edit series" if show else "Add series")
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        show = show or {}

        self.name = tk.StringVar(value=show.get("name", ""))
        self.season = tk.StringVar(value=str(show.get("season", 1)))
        self.next_episode = tk.StringVar(value=str(show.get("next_episode", 1)))
        self.quality = tk.StringVar(value=quality_label(show.get("quality", "")) if show else "1080p")
        self.min_seeders = tk.StringVar(value=str(show.get("min_seeders", 3)))
        skip = show.get("must_not_contain")
        self.skip = tk.StringVar(value=DEFAULT_SKIP if skip is None else ", ".join(skip))
        self.folder = tk.StringVar(value=show.get("save_path", ""))
        self.enabled = tk.BooleanVar(value=show.get("enabled", True))
        # New series get this switched on; existing ones keep their setting.
        self.remove_done = tk.BooleanVar(value=show.get("remove_when_done", not show))
        self.part_of_title = tk.BooleanVar(value=show.get("part_of_title", False))

        form = ttk.Frame(self, padding=16)
        form.grid(sticky="nsew")
        form.columnconfigure(1, weight=1)

        def row(index, label, widget, hint=None):
            ttk.Label(form, text=label).grid(row=index * 2, column=0, sticky="w", pady=(6, 0))
            widget.grid(row=index * 2, column=1, sticky="ew", padx=(12, 0), pady=(6, 0))
            if hint:
                ttk.Label(form, text=hint, foreground="#666").grid(
                    row=index * 2 + 1, column=1, sticky="w", padx=(12, 0)
                )

        name_entry = ttk.Entry(form, textvariable=self.name, width=42)
        row(0, "Series name", name_entry, "As it appears in torrent names, e.g. Example Show")
        row(1, "Season", ttk.Spinbox(form, from_=1, to=99, textvariable=self.season, width=6))
        row(
            2,
            "Next episode to get",
            ttk.Spinbox(form, from_=1, to=999, textvariable=self.next_episode, width=6),
            "Earlier episodes are never downloaded",
        )
        row(
            3,
            "Quality",
            ttk.Combobox(form, textvariable=self.quality, values=[label for label, _ in QUALITY_CHOICES],
                         state="readonly", width=12),
        )
        row(
            4,
            "Minimum seeders",
            ttk.Spinbox(form, from_=0, to=9999, textvariable=self.min_seeders, width=6),
        )
        row(5, "Skip releases with", ttk.Entry(form, textvariable=self.skip), "Words separated by commas")

        folder_row = ttk.Frame(form)
        folder_row.columnconfigure(0, weight=1)
        ttk.Entry(folder_row, textvariable=self.folder).grid(row=0, column=0, sticky="ew")
        ttk.Button(folder_row, text="Browse…", command=self.browse).grid(row=0, column=1, padx=(6, 0))
        row(6, "Download folder", folder_row, "Leave empty to use qBittorrent's default folder")

        ttk.Checkbutton(form, text="Watch this series", variable=self.enabled).grid(
            row=14, column=1, sticky="w", padx=(12, 0), pady=(10, 0)
        )

        ttk.Checkbutton(
            form,
            text="Remove from qBittorrent when finished (files are kept)",
            variable=self.remove_done,
        ).grid(row=15, column=1, sticky="w", padx=(12, 0), pady=(4, 0))

        ttk.Checkbutton(
            form,
            text="Name is only part of a longer title",
            variable=self.part_of_title,
        ).grid(row=16, column=1, sticky="w", padx=(12, 0), pady=(4, 0))
        ttk.Label(
            form,
            text="Tick if releases use a longer title that contains this name",
            foreground="#666",
        ).grid(row=17, column=1, sticky="w", padx=(32, 0))

        buttons = ttk.Frame(form)
        buttons.grid(row=18, column=0, columnspan=2, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(buttons, text="Save", command=self.save, default="active").grid(row=0, column=1)

        self.bind("<Return>", lambda event: self.save())
        self.bind("<Escape>", lambda event: self.destroy())
        name_entry.focus_set()
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.grab_set()

    def browse(self):
        chosen = filedialog.askdirectory(parent=self, initialdir=self.folder.get() or None)
        if chosen:
            self.folder.set(os.path.normpath(chosen))

    def save(self):
        name = self.name.get().strip()
        if not name:
            messagebox.showwarning("Series name", "Type the name of the series.", parent=self)
            return
        try:
            season = int(self.season.get())
            next_episode = int(self.next_episode.get())
            min_seeders = int(self.min_seeders.get())
            if season < 1 or next_episode < 1 or min_seeders < 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning(
                "Numbers", "Season, episode and seeders must be whole numbers.", parent=self
            )
            return
        self.result = {
            "name": name,
            "season": season,
            "next_episode": next_episode,
            "quality": quality_value(self.quality.get()),
            "min_seeders": min_seeders,
            "must_not_contain": [w.strip() for w in self.skip.get().split(",") if w.strip()],
            "save_path": self.folder.get().strip(),
            "enabled": self.enabled.get(),
            "remove_when_done": self.remove_done.get(),
            "part_of_title": self.part_of_title.get(),
        }
        self.destroy()


class MovieDialog(tk.Toplevel):
    """Form for adding or editing one movie. Result is in self.result."""

    def __init__(self, parent, movie=None, default_folder=""):
        super().__init__(parent)
        self.title("Edit movie" if movie else "Add movie")
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        movie = movie or {}

        self.name = tk.StringVar(value=movie.get("name", ""))
        self.year = tk.StringVar(value=str(movie.get("year", datetime.now().year)))
        self.quality = tk.StringVar(value=quality_label(movie.get("quality", "1080p")))
        self.min_seeders = tk.StringVar(value=str(movie.get("min_seeders", MOVIE_DEFAULTS["min_seeders"])))
        self.skip = tk.StringVar(value=", ".join(movie.get("must_not_contain", MOVIE_DEFAULTS["must_not_contain"])))
        self.folder = tk.StringVar(value=movie.get("save_path", ""))
        self.enabled = tk.BooleanVar(value=movie.get("enabled", True))
        self.remove_done = tk.BooleanVar(value=movie.get("remove_when_done", True))
        self.done = tk.BooleanVar(value=movie.get("done", False))

        form = ttk.Frame(self, padding=16)
        form.grid(sticky="nsew")
        form.columnconfigure(1, weight=1)

        def row(index, label, widget, hint=None):
            ttk.Label(form, text=label).grid(row=index * 2, column=0, sticky="w", pady=(6, 0))
            widget.grid(row=index * 2, column=1, sticky="ew", padx=(12, 0), pady=(6, 0))
            if hint:
                ttk.Label(form, text=hint, foreground="#666").grid(
                    row=index * 2 + 1, column=1, sticky="w", padx=(12, 0))

        name_entry = ttk.Entry(form, textvariable=self.name, width=42)
        row(0, "Movie title", name_entry, "As it appears in torrent names, e.g. Example Movie")
        row(1, "Year", ttk.Spinbox(form, from_=1900, to=2100, textvariable=self.year, width=6),
            "Required: tells remakes and sequels apart")
        row(2, "Quality", ttk.Combobox(form, textvariable=self.quality,
                                       values=[label for label, _ in QUALITY_CHOICES],
                                       state="readonly", width=12))
        row(3, "Minimum seeders", ttk.Spinbox(form, from_=0, to=9999, textvariable=self.min_seeders, width=6))
        row(4, "Skip releases with", ttk.Entry(form, textvariable=self.skip),
            "Words separated by commas; skips cinema recordings")
        folder_row = ttk.Frame(form)
        folder_row.columnconfigure(0, weight=1)
        ttk.Entry(folder_row, textvariable=self.folder).grid(row=0, column=0, sticky="ew")
        ttk.Button(folder_row, text="Browse…", command=self.browse).grid(row=0, column=1, padx=(6, 0))
        row(5, "Download folder", folder_row,
            f"Leave empty for {default_folder}\\Title (Year)" if default_folder
            else "Leave empty to use qBittorrent's default folder")
        ttk.Checkbutton(form, text="Watch this movie", variable=self.enabled).grid(
            row=14, column=1, sticky="w", padx=(12, 0), pady=(10, 0))
        ttk.Checkbutton(form, text="Remove from qBittorrent when finished (files are kept)",
                        variable=self.remove_done).grid(row=15, column=1, sticky="w", padx=(12, 0), pady=(4, 0))
        if movie:
            ttk.Checkbutton(form, text="Downloaded (untick to look for it again)",
                            variable=self.done).grid(row=16, column=1, sticky="w", padx=(12, 0), pady=(4, 0))

        buttons = ttk.Frame(form)
        buttons.grid(row=18, column=0, columnspan=2, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(buttons, text="Save", command=self.save, default="active").grid(row=0, column=1)
        self.bind("<Return>", lambda event: self.save())
        self.bind("<Escape>", lambda event: self.destroy())
        name_entry.focus_set()
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.grab_set()

    def browse(self):
        chosen = filedialog.askdirectory(parent=self, initialdir=self.folder.get() or None)
        if chosen:
            self.folder.set(os.path.normpath(chosen))

    def save(self):
        name = self.name.get().strip()
        if not name:
            messagebox.showwarning("Movie title", "Type the title of the movie.", parent=self)
            return
        try:
            year = int(self.year.get())
            min_seeders = int(self.min_seeders.get())
            if year < 1880 or min_seeders < 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Numbers", "Year and seeders must be whole numbers.", parent=self)
            return
        self.result = {
            "name": name,
            "year": year,
            "quality": quality_value(self.quality.get()),
            "min_seeders": min_seeders,
            "must_not_contain": [w.strip() for w in self.skip.get().split(",") if w.strip()],
            "save_path": self.folder.get().strip(),
            "enabled": self.enabled.get(),
            "remove_when_done": self.remove_done.get(),
            "done": self.done.get(),
        }
        self.destroy()


def set_autostart(on):
    """Add or remove the watcher from the programs Windows starts at sign-in."""
    if winreg is None:
        return
    python = Path(sys.executable).with_name("pythonw.exe")
    if not python.exists():
        python = Path(sys.executable)
    command = f'"{python}" "{Path(__file__).resolve()}" --background'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        try:
            winreg.DeleteValue(key, OLD_RUN_NAME)
        except FileNotFoundError:
            pass
        if on:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass


def autostart_registered():
    """True if Windows is set to start the watcher at sign-in."""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_NAME)
        return True
    except OSError:
        return False


class App(tk.Tk):
    def __init__(self, background=False, standalone=True, store=None):
        """background: start hidden. standalone=False (tests) skips autostart,
        the single-instance listener and the qBittorrent monitor."""
        super().__init__()
        if background:
            self.withdraw()
        self.title("qBittorrent Watcher")
        self.scale = self.winfo_fpixels("1i") / 96
        self.geometry(f"{self.px(900)}x{self.px(600)}")
        self.minsize(self.px(700), self.px(450))
        self.store = store if store is not None else ConfigStore()
        self.engine = WatcherEngine(self.store)
        self.shows = []
        self.downloads = []
        self.checking = False
        self.config_mtime = None
        self.config_retry = False
        self.config_error = None
        self.search_plugins_enabled = None
        self.log_mtime = None

        toolbar = ttk.Frame(self, padding=(12, 10, 12, 6))
        toolbar.pack(fill="x")
        self.add_button = ttk.Button(toolbar, text="Add series…", command=self.add)
        self.add_button.pack(side="left")
        self.edit_button = ttk.Button(toolbar, text="Edit…", command=self.edit)
        self.edit_button.pack(side="left", padx=(6, 0))
        self.pause_button = ttk.Button(toolbar, text="Pause", command=self.toggle)
        self.pause_button.pack(side="left", padx=(6, 0))
        self.remove_button = ttk.Button(toolbar, text="Remove", command=self.remove)
        self.remove_button.pack(side="left", padx=(6, 0))
        self.check_button = ttk.Button(toolbar, text="Check now",
                                       command=lambda: self.check_now(manual=True))
        self.check_button.pack(side="right")
        self.saved_limit = 0
        self.max_downloads = tk.StringVar(value="0")
        self.limit_box = ttk.Spinbox(
            toolbar,
            from_=0,
            to=20,
            width=3,
            textvariable=self.max_downloads,
            command=self.limit_changed,
        )
        self.limit_box.pack(side="right", padx=(0, 16))
        self.limit_box.bind("<Return>", self.limit_changed)
        self.limit_box.bind("<FocusOut>", self.limit_changed)
        ttk.Label(toolbar, text="Downloads at a time (0 = no limit)").pack(
            side="right", padx=(0, 6)
        )

        self.qb_online = None  # None until the first look, then True or False
        self.last_check = 0.0
        self.autostart = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="")
        bottom = ttk.Frame(self, padding=(12, 4))
        bottom.pack(side="bottom", fill="x")
        ttk.Label(bottom, textvariable=self.status, foreground="#555").pack(side="left")
        ttk.Checkbutton(
            bottom,
            text="Start with Windows and open with qBittorrent",
            variable=self.autostart,
            command=self.autostart_changed,
        ).pack(side="right")

        panes = ttk.PanedWindow(self, orient="vertical")
        panes.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        self.panes = panes
        self.plugin_notice = ttk.Frame(self, padding=(12, 0, 12, 8))
        ttk.Label(self.plugin_notice, text=NO_SEARCH_PLUGINS, foreground="#9a5700").pack(side="left")
        ttk.Button(self.plugin_notice, text="How to fix…", command=self.search_plugin_help).pack(side="right")

        columns = {
            "name": ("Series", 200, "w"),
            "season": ("Season", 60, "center"),
            "next": ("Waiting for", 90, "center"),
            "got": ("Downloaded", 150, "w"),
            "quality": ("Quality", 70, "center"),
            "state": ("Status", 80, "center"),
            "folder": ("Download folder", 220, "w"),
        }
        self.upper = ttk.Notebook(panes)
        panes.add(self.upper, weight=3)
        self.table = self.make_tree(self.upper, columns, height=7, notebook="Series")
        self.table.bind("<<TreeviewSelect>>", lambda event: self.selection_changed())
        self.table.bind("<Double-1>", lambda event: self.edit())

        movies_tab = ttk.Frame(self.upper)
        folder_row = ttk.Frame(movies_tab, padding=(0, 6, 0, 6))
        folder_row.pack(fill="x")
        ttk.Label(folder_row, text="Movie folder").pack(side="left")
        self.movie_folder = tk.StringVar(value="")
        self.saved_movie_folder = ""
        self.movie_folder_box = ttk.Entry(folder_row, textvariable=self.movie_folder)
        self.movie_folder_box.pack(side="left", fill="x", expand=True, padx=(8, 6))
        self.movie_folder_box.bind("<Return>", lambda event: self.movie_folder_changed())
        self.movie_folder_box.bind("<FocusOut>", lambda event: self.movie_folder_changed())
        ttk.Button(folder_row, text="Browse…", command=self.browse_movie_folder).pack(side="left")
        movie_columns = {
            "name": ("Movie", 240, "w"),
            "year": ("Year", 60, "center"),
            "quality": ("Quality", 90, "center"),
            "state": ("Status", 100, "center"),
            "folder": ("Download folder", 260, "w"),
        }
        self.movie_table = self.make_tree(movies_tab, movie_columns, height=7)
        self.movie_table.bind("<<TreeviewSelect>>", lambda event: self.selection_changed())
        self.movie_table.bind("<Double-1>", lambda event: self.edit())
        self.upper.add(movies_tab, text="Movies")
        self.upper.bind("<<NotebookTabChanged>>", lambda event: self.selection_changed())
        self.movies = []

        lower = ttk.Notebook(panes)
        panes.add(lower, weight=2)
        history_columns = {
            "episode": ("Episode", 80, "center"),
            "added": ("Added", 130, "center"),
            "title": ("Release", 560, "w"),
        }
        self.history = self.make_tree(lower, history_columns, height=6, notebook="Downloaded")
        log_frame = ttk.Frame(lower)
        self.log_text = tk.Text(log_frame, wrap="none", state="disabled", height=8, font=("Consolas", 9))
        log_scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)
        lower.add(log_frame, text="Activity")

        # Background threads must not touch the window; they queue work here.
        self.results = queue.Queue()
        self.refresh(force=True)
        self.pump()
        self.poll()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        if standalone:
            self.start_background_work()

    def start_background_work(self):
        """Autostart, the single-instance listener and the qBittorrent monitor."""
        try:
            config = self.store.load()
        except (OSError, ValueError):
            # The saved choice is unknown, so change nothing: show what
            # Windows has now, and leave both it and the list alone.
            self.autostart.set(autostart_registered())
        else:
            # On the very first start, switch autostart on; after that, respect it.
            wanted = config.get("autostart", True)
            try:
                set_autostart(wanted)  # also refreshes the path if the folder moved
            except OSError:
                wanted = False
            self.autostart.set(wanted)
            if "autostart" not in config:
                self.remember_autostart(wanted)

        try:
            server = socket.socket()
            server.bind(("127.0.0.1", INSTANCE_PORT))
            server.listen(5)
            threading.Thread(target=self.listen, args=(server,), daemon=True).start()
        except OSError:
            pass
        threading.Thread(target=self.monitor, daemon=True).start()

    def px(self, value):
        return int(value * self.scale)

    def make_tree(self, parent, columns, height, notebook=None):
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(frame, columns=list(columns), show="headings", height=height, selectmode="browse")
        for key, (heading, width, anchor) in columns.items():
            tree.heading(key, text=heading, anchor=anchor)
            tree.column(key, width=self.px(width), anchor=anchor, stretch=key in ("folder", "title"))
        scroll = ttk.Scrollbar(frame, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        if notebook:
            parent.add(frame, text=notebook)
        elif isinstance(parent, ttk.PanedWindow):
            parent.add(frame, weight=3)
        else:
            frame.pack(fill="both", expand=True)
        return tree

    # ---- data -----------------------------------------------------------

    def movies_shown(self):
        """True while the Movies tab is the one in front."""
        try:
            return self.upper.index(self.upper.select()) == 1
        except tk.TclError:
            return False

    def selected_movie(self):
        chosen = self.movie_table.selection()
        if not chosen:
            return None
        return next((movie for movie in self.movies if movie["id"] == chosen[0]), None)

    def selected(self):
        """The selected series as stored in self.shows, or None."""
        chosen = self.table.selection()
        if not chosen:
            return None
        for show in self.shows:
            if series_key(show) == chosen[0]:
                return show
        return None

    def refresh(self, force=False):
        """Reload series.json and the log if they changed on disk."""
        try:
            try:
                mtime = self.store.path.stat().st_mtime
            except FileNotFoundError:
                mtime = None
            if force or self.config_retry or mtime != self.config_mtime:
                config = self.store.load()
                # Failed reads must neither replace the displayed snapshot nor
                # prevent the next poll from retrying an unchanged file.
                self.config_mtime = mtime
                self.config_retry = False
                self.config_error = None
                self.shows = config["series"]
                self.downloads = config["downloads"]
                self.saved_limit = int(config.get("max_downloads", 0))
                if not self.editing_limit():
                    self.max_downloads.set(str(self.saved_limit))
                keep = self.table.selection()
                self.table.delete(*self.table.get_children())
                for show in self.shows:
                    got = [d["episode"] for d in self.downloads if d.get("series_id") == show["id"]
                           and d["season"] == show["season"] and d["episode"] is not None]
                    self.table.insert(
                        "",
                        "end",
                        iid=series_key(show),
                        values=(
                            show["name"],
                            show["season"],
                            f"S{show['season']:02d}E{show['next_episode']:02d}",
                            episode_ranges(got) or "none yet",
                            quality_label(show.get("quality", "")),
                            "Watching" if show.get("enabled", True) else "Paused",
                            show.get("save_path") or "(qBittorrent default)",
                        ),
                    )
                if keep and self.table.exists(keep[0]):
                    self.table.selection_set(keep[0])
                elif self.shows:
                    self.table.selection_set(series_key(self.shows[0]))
                self.fill_movies(config)
                self.selection_changed()
                self.show_status()
        except (OSError, ValueError) as error:
            self.config_retry = True
            self.config_error = f"Could not read series.json: {error}"
            self.show_status()

        try:
            log_mtime = self.engine.log_file.stat().st_mtime
        except OSError:
            log_mtime = None
        if force or log_mtime != self.log_mtime:
            self.log_mtime = log_mtime
            try:
                lines = self.engine.log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                lines = []
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.insert("end", "\n".join(display_date(line) for line in lines[-300:]))
            self.log_text.configure(state="disabled")
            self.log_text.see("end")

    def fill_movies(self, config):
        self.movies = config["movies"]
        self.saved_movie_folder = config.get("movie_folder", "")
        try:
            editing = self.focus_get() is self.movie_folder_box
        except KeyError:
            editing = False
        if not editing:
            self.movie_folder.set(self.saved_movie_folder)
        keep = self.movie_table.selection()
        self.movie_table.delete(*self.movie_table.get_children())
        for movie in self.movies:
            state = ("Downloaded" if movie["done"] else
                     "Waiting" if movie["enabled"] else "Paused")
            self.movie_table.insert("", "end", iid=movie["id"], values=(
                movie["name"], movie["year"], quality_label(movie["quality"]), state,
                movie_destination(movie, config)[0] or "(qBittorrent default)"))
        if keep and self.movie_table.exists(keep[0]):
            self.movie_table.selection_set(keep[0])
        elif self.movies:
            self.movie_table.selection_set(self.movies[0]["id"])

    def movie_folder_changed(self):
        folder = self.movie_folder.get().strip()
        if folder == self.saved_movie_folder:
            return
        if self.change(lambda: self.store.set_setting("movie_folder", folder)):
            self.saved_movie_folder = folder

    def browse_movie_folder(self):
        chosen = filedialog.askdirectory(parent=self, initialdir=self.movie_folder.get() or None)
        if chosen:
            self.movie_folder.set(os.path.normpath(chosen))
            self.movie_folder_changed()

    def selection_changed(self):
        if self.movies_shown():
            self.add_button.configure(text="Add movie…")
            movie = self.selected_movie()
            state = "normal" if movie else "disabled"
            for button in (self.edit_button, self.pause_button, self.remove_button):
                button.configure(state=state)
            self.pause_button.configure(
                text="Resume" if movie and not movie["enabled"] else "Pause")
            self.history.heading("episode", text="Year")
            self.history.delete(*self.history.get_children())
            if movie:
                for item in sorted((d for d in self.downloads if d.get("movie_id") == movie["id"]),
                                   key=lambda d: d["added"], reverse=True):
                    self.history.insert("", "end", values=(
                        movie["year"], display_date(item["added"]), item["title"]))
            return
        self.add_button.configure(text="Add series…")
        self.history.heading("episode", text="Episode")
        show = self.selected()
        state = "normal" if show else "disabled"
        for button in (self.edit_button, self.pause_button, self.remove_button):
            button.configure(state=state)
        self.pause_button.configure(
            text="Resume" if show and not show.get("enabled", True) else "Pause"
        )
        self.history.delete(*self.history.get_children())
        if show:
            records = [d for d in self.downloads if d.get("series_id") == show["id"]]
            for item in sorted(records, key=lambda d: (d["season"] or 0, d["episode"] or 0, d["added"]), reverse=True):
                tag = (f"S{item['season']:02d}E{item['episode']:02d}"
                       if item["season"] is not None and item["episode"] is not None
                       else item.get("label", ""))
                self.history.insert("", "end", values=(tag, display_date(item["added"]), item["title"]))

    def change(self, action):
        """Run a storage command and refresh the displayed snapshot."""
        try:
            action()
        except (OSError, ValueError) as error:
            messagebox.showerror("qBittorrent Watcher", f"Could not save series.json:\n{error}", parent=self)
            return False
        self.refresh(force=True)
        return True

    # ---- actions --------------------------------------------------------

    def add(self):
        if self.movies_shown():
            dialog = MovieDialog(self, default_folder=self.saved_movie_folder)
            self.wait_window(dialog)
            created = []
            if dialog.result and self.change(lambda: created.append(self.store.add_movie(dialog.result))):
                self.movie_table.selection_set(created[0])
            return
        dialog = SeriesDialog(self)
        self.wait_window(dialog)
        if not dialog.result:
            return
        created = []
        if self.change(lambda: created.append(self.store.add_series(dialog.result))):
            self.table.selection_set(created[0])

    def edit(self):
        if self.movies_shown():
            movie = self.selected_movie()
            if movie:
                dialog = MovieDialog(self, movie, default_folder=self.saved_movie_folder)
                self.wait_window(dialog)
                if dialog.result:
                    self.change(lambda: self.store.edit_movie(movie["id"], dialog.result,
                                                              original=movie))
            return
        old = self.selected()
        if not old:
            return
        dialog = SeriesDialog(self, old)
        self.wait_window(dialog)
        if dialog.result:
            self.change(lambda: self.store.edit_series(old["id"], dialog.result, original=old))

    def toggle(self):
        if self.movies_shown():
            movie = self.selected_movie()
            if movie:
                self.change(lambda: self.store.toggle_movie(movie["id"]))
            return
        show = self.selected()
        if show:
            self.change(lambda: self.store.toggle_series(show["id"]))

    def remove(self):
        if self.movies_shown():
            movie = self.selected_movie()
            if movie and messagebox.askyesno(
                    "Remove movie",
                    f"Stop watching {movie['name']} ({movie['year']})?\n\n"
                    "Downloaded files and torrents are not touched.",
                    parent=self):
                self.change(lambda: self.store.remove_movie(movie["id"]))
            return
        show = self.selected()
        if not show:
            return
        if messagebox.askyesno(
            "Remove series",
            f"Stop watching {show['name']} season {show['season']}?\n\n"
            "Its download history is forgotten. Downloaded files and torrents are not touched.",
            parent=self,
        ):
            self.change(lambda: self.store.remove_series(show["id"]))

    def editing_limit(self):
        try:
            return self.focus_get() is self.limit_box
        except KeyError:
            return False

    def limit_changed(self, event=None):
        """Save the downloads-at-a-time setting and pass it to qBittorrent."""
        try:
            limit = max(0, min(20, int(self.max_downloads.get())))
        except ValueError:
            limit = self.saved_limit
        self.max_downloads.set(str(limit))
        if limit == self.saved_limit:
            return
        try:
            self.store.set_setting("max_downloads", limit)
        except (OSError, ValueError) as error:
            messagebox.showerror("qBittorrent Watcher", f"Could not save the setting:\n{error}", parent=self)
            return
        self.saved_limit = limit

        def work():
            try:
                self.engine.apply_limit()
            except (urllib.error.URLError, OSError, ValueError):
                pass  # qBittorrent is closed; the next check applies it

        threading.Thread(target=work, daemon=True).start()

    def check_now(self, manual=False):
        """Start a check in the background.

        Automatic checks wait while no search plugin is known to be enabled.
        A click on Check now always runs: the plugin state shown may be a few
        seconds old, and the check itself finds out and reports it.
        """
        if self.checking:
            return
        if not manual and self.search_plugins_enabled is False:
            return
        self.checking = True
        self.last_check = time.time()
        self.check_button.configure(state="disabled", text="Checking…")
        self.show_status()

        def work():
            python = Path(sys.executable).with_name("python.exe")
            if not python.exists():
                python = Path(sys.executable)
            try:
                done = subprocess.run(
                    [str(python), str(HERE / "watcher.py"), "--config", str(self.store.path)],
                    capture_output=True,
                    encoding="utf-8",
                    env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                    creationflags=NO_WINDOW,
                )
                no_plugins = done.returncode == NO_PLUGINS_EXIT
                problem = (done.stderr.strip()
                           if done.returncode not in (0, 1, NO_PLUGINS_EXIT) else "")
            except OSError as error:
                no_plugins = False
                problem = str(error)
            self.results.put(lambda: self.check_finished(problem, no_plugins, manual))

        threading.Thread(target=work, daemon=True).start()

    def check_finished(self, problem, no_plugins=False, manual=False):
        self.checking = False
        self.check_button.configure(state="normal", text="Check now")
        if no_plugins:
            # The check found no enabled search plugin: show the notice now,
            # rather than waiting for the next probe.
            self.search_plugins_enabled = False
            self.update_plugin_notice()
        self.refresh(force=True)
        self.show_status()
        if problem:
            messagebox.showerror("Check failed", problem[-1500:], parent=self)
        elif no_plugins and manual:
            self.search_plugin_help()

    def update_plugin_notice(self):
        if self.search_plugins_enabled is False:
            self.plugin_notice.pack(fill="x", before=self.panes)
        else:
            self.plugin_notice.pack_forget()

    # ---- background updates ----------------------------------------------

    def search_plugin_help(self):
        messagebox.showinfo("Enable search plugins", SEARCH_PLUGIN_HELP, parent=self)

    def show_status(self):
        if self.config_error:
            text = self.config_error
        elif self.checking:
            text = "Checking for new episodes…"
        elif self.qb_online and self.search_plugins_enabled is False:
            text = "Automatic searches are waiting for an enabled search plugin."
        elif self.qb_online:
            due = datetime.fromtimestamp(self.last_check + CHECK_EVERY)
            text = f"qBittorrent is running. Next automatic check at {due:%H:%M}."
        elif self.qb_online is False:
            text = "qBittorrent is not running. A check starts as soon as it opens."
        else:
            text = ""
        self.status.set(text)

    def monitor(self):
        """Schedule engine probes and cleanup without touching window widgets."""
        last_tidy = 0.0
        while True:
            tidy = time.monotonic() - last_tidy >= TIDY_EVERY
            error = None
            plugins_enabled = None
            try:
                online = self.engine.poll(tidy=tidy)
                plugins_enabled = self.engine.search_plugins_enabled
            except (OSError, ValueError) as problem:
                online = None
                error = f"Could not read series.json: {problem}"
            if tidy:
                last_tidy = time.monotonic()
            self.results.put(lambda online=online, error=error, plugins_enabled=plugins_enabled:
                             self.qbittorrent_seen(online, error, plugins_enabled))
            time.sleep(POLL_EVERY)

    def qbittorrent_seen(self, online, error=None, plugins_enabled=None):
        if error:
            self.config_retry = True
            self.config_error = error
        if self.config_error:
            self.show_status()
            return
        was_online = self.qb_online
        previous_plugins = self.search_plugins_enabled
        self.qb_online = online
        if not online:
            self.search_plugins_enabled = None
        elif plugins_enabled is not None:
            self.search_plugins_enabled = plugins_enabled
        # A probe that could not read the plugin list (None) keeps the last
        # known answer, so one timeout neither hides the notice nor starts a
        # check while plugins are still disabled.
        self.update_plugin_notice()
        plugins = self.search_plugins_enabled
        if online and not was_online:
            # qBittorrent has just opened (or the watcher has): show and check.
            self.show_window()
            if plugins is not False:
                self.check_now()
        elif online and previous_plugins is False and plugins is True:
            self.check_now()  # a plugin has just been enabled: resume at once
        elif online and plugins is not False and time.time() - self.last_check >= CHECK_EVERY:
            self.check_now()
        elif was_online and not online and self.autostart.get():
            self.withdraw()  # qBittorrent closed; wait out of sight
        self.show_status()

    def show_window(self):
        self.deiconify()
        self.lift()

    def listen(self, server):
        """Background thread: a second launch asks this one to show itself."""
        while True:
            try:
                connection, _ = server.accept()
                connection.close()
            except OSError:
                return
            self.results.put(self.show_window)

    def on_close(self):
        if self.autostart.get():
            self.withdraw()  # keep watching in the background
        else:
            self.destroy()

    def autostart_changed(self):
        wanted = self.autostart.get()
        try:
            set_autostart(wanted)
        except OSError as error:
            messagebox.showerror("qBittorrent Watcher", f"Could not change the setting:\n{error}", parent=self)
            self.autostart.set(not wanted)
            return
        self.remember_autostart(wanted)

    def remember_autostart(self, wanted):
        try:
            self.store.set_setting("autostart", wanted)
        except (OSError, ValueError):
            pass

    def pump(self):
        """Run whatever the background threads have finished."""
        try:
            while True:
                self.results.get_nowait()()
        except queue.Empty:
            pass
        self.after(200, self.pump)

    def poll(self):
        self.refresh()
        self.after(2000, self.poll)


def already_running():
    """If another watcher window is running, ask it to show itself."""
    try:
        socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=1).close()
        return True
    except OSError:
        return False


if __name__ == "__main__":
    if already_running():
        sys.exit(0)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on scaled screens
    except (AttributeError, OSError):
        pass
    App(background="--background" in sys.argv).mainloop()
