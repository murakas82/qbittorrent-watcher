"""Window for managing the series watcher.

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

import watcher

CHECK_EVERY = 3600  # seconds between automatic checks while qBittorrent runs
POLL_EVERY = 5  # seconds between looks for qBittorrent
INSTANCE_PORT = 47821  # local port used to keep a single window running
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "qBittorrent Series Watcher"
QUALITIES = ["Any", "2160p", "1080p", "720p", "480p"]
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
    return f"{show['name'].strip().lower()}|{show['season']}"


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
        self.quality = tk.StringVar(value=show.get("quality") or "Any" if show else "1080p")
        self.min_seeders = tk.StringVar(value=str(show.get("min_seeders", 3)))
        skip = show.get("must_not_contain")
        self.skip = tk.StringVar(value=DEFAULT_SKIP if skip is None else ", ".join(skip))
        self.folder = tk.StringVar(value=show.get("save_path", ""))
        self.enabled = tk.BooleanVar(value=show.get("enabled", True))
        # New series get this switched on; existing ones keep their setting.
        self.remove_done = tk.BooleanVar(value=show.get("remove_when_done", not show))

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
            ttk.Combobox(form, textvariable=self.quality, values=QUALITIES, state="readonly", width=10),
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

        buttons = ttk.Frame(form)
        buttons.grid(row=16, column=0, columnspan=2, sticky="e", pady=(16, 0))
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
        quality = self.quality.get()
        self.result = {
            "name": name,
            "season": season,
            "next_episode": next_episode,
            "quality": "" if quality == "Any" else quality,
            "min_seeders": min_seeders,
            "must_not_contain": [w.strip() for w in self.skip.get().split(",") if w.strip()],
            "save_path": self.folder.get().strip(),
            "enabled": self.enabled.get(),
            "remove_when_done": self.remove_done.get(),
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
        if on:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass


class App(tk.Tk):
    def __init__(self, background=False, standalone=True):
        """background: start hidden. standalone=False (tests) skips autostart,
        the single-instance listener and the qBittorrent monitor."""
        super().__init__()
        self.title("Series Watcher")
        self.scale = self.winfo_fpixels("1i") / 96
        self.geometry(f"{self.px(900)}x{self.px(600)}")
        self.minsize(self.px(700), self.px(450))
        self.shows = []
        self.checking = False
        self.config_mtime = None
        self.log_mtime = None

        toolbar = ttk.Frame(self, padding=(12, 10, 12, 6))
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Add series…", command=self.add).pack(side="left")
        self.edit_button = ttk.Button(toolbar, text="Edit…", command=self.edit)
        self.edit_button.pack(side="left", padx=(6, 0))
        self.pause_button = ttk.Button(toolbar, text="Pause", command=self.toggle)
        self.pause_button.pack(side="left", padx=(6, 0))
        self.remove_button = ttk.Button(toolbar, text="Remove", command=self.remove)
        self.remove_button.pack(side="left", padx=(6, 0))
        self.check_button = ttk.Button(toolbar, text="Check now", command=self.check_now)
        self.check_button.pack(side="right")

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

        columns = {
            "name": ("Series", 200, "w"),
            "season": ("Season", 60, "center"),
            "next": ("Waiting for", 90, "center"),
            "got": ("Downloaded", 150, "w"),
            "quality": ("Quality", 70, "center"),
            "state": ("Status", 80, "center"),
            "folder": ("Download folder", 220, "w"),
        }
        self.table = self.make_tree(panes, columns, height=7)
        self.table.bind("<<TreeviewSelect>>", lambda event: self.selection_changed())
        self.table.bind("<Double-1>", lambda event: self.edit())

        lower = ttk.Notebook(panes)
        panes.add(lower, weight=2)
        history_columns = {
            "episode": ("Episode", 80, "center"),
            "added": ("Added", 130, "center"),
            "title": ("Release", 560, "w"),
        }
        self.history = self.make_tree(lower, history_columns, height=6, notebook="Downloaded episodes")
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
        if background:
            self.withdraw()
        if standalone:
            self.start_background_work()

    def start_background_work(self):
        """Autostart, the single-instance listener and the qBittorrent monitor."""
        try:
            config = watcher.load_config()
        except (OSError, ValueError):
            config = {}
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
        else:
            parent.add(frame, weight=3)
        return tree

    # ---- data -----------------------------------------------------------

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
            mtime = watcher.CONFIG_FILE.stat().st_mtime
        except OSError:
            mtime = None
        if force or mtime != self.config_mtime:
            self.config_mtime = mtime
            try:
                self.shows = watcher.load_config()["series"] if mtime else []
            except (OSError, ValueError) as error:
                self.shows = []
                self.status.set(f"Could not read series.json: {error}")
            keep = self.table.selection()
            self.table.delete(*self.table.get_children())
            for show in self.shows:
                got = [item["episode"] for item in show.get("history", [])]
                self.table.insert(
                    "",
                    "end",
                    iid=series_key(show),
                    values=(
                        show["name"],
                        show["season"],
                        f"S{show['season']:02d}E{show['next_episode']:02d}",
                        episode_ranges(got) or "none yet",
                        show.get("quality") or "Any",
                        "Watching" if show.get("enabled", True) else "Paused",
                        show.get("save_path") or "(qBittorrent default)",
                    ),
                )
            if keep and self.table.exists(keep[0]):
                self.table.selection_set(keep[0])
            elif self.shows:
                self.table.selection_set(series_key(self.shows[0]))
            self.selection_changed()

        try:
            log_mtime = watcher.LOG_FILE.stat().st_mtime
        except OSError:
            log_mtime = None
        if force or log_mtime != self.log_mtime:
            self.log_mtime = log_mtime
            try:
                lines = watcher.LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                lines = []
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.insert("end", "\n".join(lines[-300:]))
            self.log_text.configure(state="disabled")
            self.log_text.see("end")

    def selection_changed(self):
        show = self.selected()
        state = "normal" if show else "disabled"
        for button in (self.edit_button, self.pause_button, self.remove_button):
            button.configure(state=state)
        self.pause_button.configure(
            text="Resume" if show and not show.get("enabled", True) else "Pause"
        )
        self.history.delete(*self.history.get_children())
        if show:
            for item in sorted(show.get("history", []), key=lambda i: i["episode"], reverse=True):
                self.history.insert(
                    "",
                    "end",
                    values=(
                        f"S{show['season']:02d}E{item['episode']:02d}",
                        item.get("added", ""),
                        item.get("title", ""),
                    ),
                )

    def change(self, edit):
        """Apply edit(series_list) to a fresh copy of series.json and save it."""
        try:
            config = watcher.load_config()
        except FileNotFoundError:
            config = {"qbittorrent_url": "http://127.0.0.1:8080", "series": []}
        except (OSError, ValueError) as error:
            messagebox.showerror("Series Watcher", f"Could not read series.json:\n{error}", parent=self)
            return
        edit(config["series"])
        try:
            watcher.save_config(config)
        except OSError as error:
            messagebox.showerror("Series Watcher", f"Could not save series.json:\n{error}", parent=self)
        self.refresh(force=True)

    # ---- actions --------------------------------------------------------

    def add(self):
        dialog = SeriesDialog(self)
        self.wait_window(dialog)
        new = dialog.result
        if not new:
            return
        if any(watcher.same_series(new, show) for show in self.shows):
            messagebox.showinfo(
                "Series Watcher",
                f"{new['name']} season {new['season']} is already in the list.",
                parent=self,
            )
            return
        new["history"] = []
        self.change(lambda series: series.append(new))
        self.table.selection_set(series_key(new))

    def edit(self):
        old = self.selected()
        if not old:
            return
        dialog = SeriesDialog(self, old)
        self.wait_window(dialog)
        new = dialog.result
        if not new:
            return
        if not watcher.same_series(new, old) and any(
            watcher.same_series(new, show) for show in self.shows
        ):
            messagebox.showinfo(
                "Series Watcher",
                f"{new['name']} season {new['season']} is already in the list.",
                parent=self,
            )
            return

        def apply(series):
            for entry in series:
                if watcher.same_series(entry, old):
                    if entry["season"] != new["season"]:
                        entry["history"] = []  # history belongs to the old season
                    entry.update(new)

        self.change(apply)
        if self.table.exists(series_key(new)):
            self.table.selection_set(series_key(new))

    def toggle(self):
        old = self.selected()
        if not old:
            return

        def apply(series):
            for entry in series:
                if watcher.same_series(entry, old):
                    entry["enabled"] = not entry.get("enabled", True)

        self.change(apply)

    def remove(self):
        old = self.selected()
        if not old:
            return
        if not messagebox.askyesno(
            "Remove series",
            f"Stop watching {old['name']} season {old['season']}?\n\n"
            "Its download history is forgotten. Downloaded files and torrents are not touched.",
            parent=self,
        ):
            return
        self.change(
            lambda series: series.__setitem__(
                slice(None), [e for e in series if not watcher.same_series(e, old)]
            )
        )

    def check_now(self):
        if self.checking:
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
                    [str(python), str(watcher.HERE / "watcher.py")],
                    capture_output=True,
                    text=True,
                    creationflags=NO_WINDOW,
                )
                problem = done.stderr.strip() if done.returncode not in (0, 1) else ""
            except OSError as error:
                problem = str(error)
            self.results.put(lambda: self.check_finished(problem))

        threading.Thread(target=work, daemon=True).start()

    def check_finished(self, problem):
        self.checking = False
        self.check_button.configure(state="normal", text="Check now")
        self.refresh(force=True)
        self.show_status()
        if problem:
            messagebox.showerror("Check failed", problem[-1500:], parent=self)

    # ---- background updates ----------------------------------------------

    def show_status(self):
        if self.checking:
            text = "Checking for new episodes…"
        elif self.qb_online:
            due = datetime.fromtimestamp(self.last_check + CHECK_EVERY)
            text = f"qBittorrent is running. Next automatic check at {due:%H:%M}."
        elif self.qb_online is False:
            text = "qBittorrent is not running. A check starts as soon as it opens."
        else:
            text = ""
        self.status.set(text)

    def monitor(self):
        """Background thread: keep asking whether qBittorrent is reachable."""
        while True:
            try:
                url = watcher.load_config().get("qbittorrent_url", "")
                watcher.QBittorrent(url).call("app/version", timeout=3)
                online = True
            except urllib.error.HTTPError:
                online = True  # running but refusing us; the check will log why
            except (urllib.error.URLError, OSError, ValueError):
                online = False
            self.results.put(lambda online=online: self.qbittorrent_seen(online))
            time.sleep(POLL_EVERY)

    def qbittorrent_seen(self, online):
        was_online = self.qb_online
        self.qb_online = online
        if online and not was_online:
            # qBittorrent has just opened (or the watcher has): show and check.
            self.show_window()
            self.check_now()
        elif online and time.time() - self.last_check >= CHECK_EVERY:
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
            messagebox.showerror("Series Watcher", f"Could not change the setting:\n{error}", parent=self)
            self.autostart.set(not wanted)
            return
        self.remember_autostart(wanted)

    def remember_autostart(self, wanted):
        try:
            config = watcher.load_config()
            config["autostart"] = wanted
            watcher.save_config(config)
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
