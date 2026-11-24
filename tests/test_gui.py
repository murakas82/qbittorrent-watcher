"""Exercise the real Tk widgets with an isolated store and no background work."""

import importlib.machinery
import importlib.util
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from store import ConfigStore

loader = importlib.machinery.SourceFileLoader(
    "watcher_gui", str(Path(__file__).resolve().parents[1] / "watcher_gui.pyw"))
spec = importlib.util.spec_from_loader(loader.name, loader)
gui = importlib.util.module_from_spec(spec)
loader.exec_module(gui)


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="watcher-gui-tests-")
        self.addCleanup(self.temp.cleanup)
        self.store = ConfigStore(Path(self.temp.name) / "series.json")
        self.series_id = self.store.add_series(dict(name="Example Show", season=1, next_episode=1))
        try:
            self.app = gui.App(background=True, standalone=False, store=self.store)
        except tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        self.addCleanup(self.app.destroy)

    def test_movies_tab_lists_movies_and_drives_the_toolbar(self):
        self.store.set_setting("movie_folder", r"C:\Media\Filmid")
        movie_id = self.store.add_movie(dict(name="Example Movie", year=2025, quality="2160p"))
        self.app.refresh(force=True)
        self.app.upper.select(1)
        self.app.update()
        self.assertTrue(self.app.movies_shown())
        self.assertEqual(self.app.add_button.cget("text"), "Add movie…")
        self.assertEqual(self.app.movie_table.item(movie_id, "values")[:4],
                         ("Example Movie", "2025", "4K (2160p)", "Waiting"))
        self.assertEqual(self.app.movie_folder.get(), r"C:\Media\Filmid")
        self.assertEqual(self.app.movie_table.item(movie_id, "values")[4],
                         r"C:\Media\Filmid\Example Movie (2025)")
        self.app.movie_table.selection_set(movie_id)
        self.app.update()
        self.app.toggle()
        self.assertFalse(self.store.current_movie(movie_id)["enabled"])
        self.assertEqual(self.app.movie_table.item(movie_id, "values")[3], "Paused")
        self.assertEqual(self.app.pause_button.cget("text"), "Resume")
        with patch.object(gui, "MovieDialog") as dialog, patch.object(self.app, "wait_window"):
            dialog.return_value.result = dict(name="Example Movie", year=2025, quality="1080p",
                                              done=True)
            self.app.edit()
        self.assertEqual(self.app.movie_table.item(movie_id, "values")[2:4], ("1080p", "Downloaded"))
        self.app.upper.select(0)
        self.app.update()
        self.assertEqual(self.app.add_button.cget("text"), "Add series…")
        self.assertEqual(self.store.current_entry(self.series_id)["enabled"], True)

    def test_movie_folder_box_saves_the_default_folder(self):
        self.app.movie_folder.set(r"C:\Media\Filmid")
        self.app.movie_folder_changed()
        self.assertEqual(self.store.load()["movie_folder"], r"C:\Media\Filmid")

    def test_series_dialog_offers_4k(self):
        dialog = gui.SeriesDialog(self.app)
        self.addCleanup(lambda: dialog.winfo_exists() and dialog.destroy())
        dialog.name.set("Example Show 2")
        dialog.quality.set("4K (2160p)")
        dialog.save()
        self.assertEqual(dialog.result["quality"], "2160p")

    def test_table_and_history_follow_stable_id_across_seasons(self):
        show = self.store.current_entry(self.series_id)
        self.store.record_download(show, "season one", "one")
        self.store.edit_series(self.series_id, dict(name="Renamed Show", season=2, next_episode=1))
        self.store.record_download(self.store.current_entry(self.series_id), "season two", "two")
        self.app.refresh(force=True)
        self.assertEqual(self.app.table.selection(), (self.series_id,))
        self.assertEqual(self.app.selected()["name"], "Renamed Show")
        rows = [self.app.history.item(row, "values") for row in self.app.history.get_children()]
        self.assertEqual({row[0] for row in rows}, {"S01E01", "S02E01"})

    def test_save_dialog_keeps_progress_made_while_it_was_open(self):
        original = self.store.current_entry(self.series_id)
        result = dict(original, quality="2160p")
        def wait(dialog):
            self.store.record_download(original, "episode one", "one")
        with patch.object(gui, "SeriesDialog", return_value=SimpleNamespace(result=result)), \
             patch.object(self.app, "wait_window", side_effect=wait):
            self.app.edit()
        current = self.store.current_entry(self.series_id)
        self.assertEqual(current["next_episode"], 2)
        self.assertEqual(current["quality"], "2160p")
        self.assertEqual(self.app.table.selection(), (self.series_id,))

    def test_add_toggle_and_remove_use_storage_commands(self):
        result = dict(name="Another Show", season=1, next_episode=1)
        with patch.object(gui, "SeriesDialog", return_value=SimpleNamespace(result=result)), \
             patch.object(self.app, "wait_window"):
            self.app.add()
        chosen = self.app.selected()
        self.assertEqual(chosen["name"], "Another Show")
        self.app.toggle()
        self.assertFalse(self.store.current_entry(chosen["id"])["enabled"])
        with patch.object(gui.messagebox, "askyesno", return_value=True):
            self.app.remove()
        self.assertIsNone(self.store.current_entry(chosen["id"]))
        self.assertEqual(len(self.store.load()["series"]), 1)

    def test_failed_read_keeps_history_and_retries_without_file_change(self):
        show = self.store.current_entry(self.series_id)
        self.store.record_download(show, "episode one", "one")
        self.app.refresh(force=True)
        snapshot = self.store.load()
        rows = [self.app.history.item(row, "values") for row in self.app.history.get_children()]
        table = self.app.table.get_children()
        timestamp = self.app.config_mtime
        with patch.object(self.store, "load", side_effect=[PermissionError("temporarily busy"), snapshot]) as load:
            self.app.refresh(force=True)
            self.assertTrue(self.app.config_retry)
            self.assertEqual(self.app.table.get_children(), table)
            self.assertEqual([self.app.history.item(row, "values") for row in self.app.history.get_children()], rows)
            self.assertEqual(self.app.downloads, snapshot["downloads"])
            self.app.refresh()
        self.assertEqual(load.call_count, 2)
        self.assertFalse(self.app.config_retry)
        self.assertEqual(self.app.config_mtime, timestamp)
        self.assertEqual(self.app.table.selection(), (self.series_id,))

    def test_failed_stat_keeps_series_and_retries_on_next_poll(self):
        original_stat = Path.stat
        failed = False
        def stat(path, *args, **kwargs):
            nonlocal failed
            if path == self.store.path and not failed:
                failed = True
                raise PermissionError("temporarily busy")
            return original_stat(path, *args, **kwargs)
        with patch.object(Path, "stat", autospec=True, side_effect=stat):
            self.app.refresh()
            self.assertTrue(self.app.config_retry)
            self.assertEqual(self.app.table.get_children(), (self.series_id,))
            self.app.refresh()
        self.assertFalse(self.app.config_retry)
        self.assertEqual(self.app.selected()["id"], self.series_id)

    def test_config_error_survives_probes_and_clears_after_successful_read(self):
        self.app.qb_online = True
        self.app.autostart.set(True)
        with patch.object(self.store, "load", side_effect=ValueError("season must be positive")):
            self.app.refresh(force=True)
        error = self.app.status.get()
        self.assertIn("season must be positive", error)
        with patch.object(self.app, "withdraw") as hide, patch.object(self.app, "check_now") as check:
            self.app.qbittorrent_seen(False)
            self.app.qbittorrent_seen(True)
            hide.assert_not_called()
            check.assert_not_called()
        self.app.checking = True
        self.app.show_status()
        self.assertEqual(self.app.status.get(), error)
        self.assertTrue(self.app.qb_online)
        self.app.refresh()
        self.assertIsNone(self.app.config_error)
        self.assertFalse(self.app.config_retry)
        self.assertIn("Checking", self.app.status.get())

    def test_plugin_notice_explains_setup_and_resumes_after_enabling(self):
        self.app.last_check = time.time()
        with patch.object(self.app, "check_now") as check, \
             patch.object(self.app, "show_window"), \
             patch.object(gui.messagebox, "showinfo") as help_message:
            self.app.qbittorrent_seen(True, plugins_enabled=False)
            self.assertEqual(self.app.plugin_notice.winfo_manager(), "pack")
            self.assertIn("waiting for an enabled search plugin", self.app.status.get())
            self.app.qbittorrent_seen(True, plugins_enabled=False)
            check.assert_not_called()
            help_message.assert_not_called()
            self.app.search_plugin_help()
            self.assertIn("View > Search Engine", help_message.call_args.args[1])
            self.assertIn("Install a new one", help_message.call_args.args[1])
            self.app.qbittorrent_seen(True, plugins_enabled=True)
            self.assertEqual(self.app.plugin_notice.winfo_manager(), "")
            check.assert_called_once()

    def test_automatic_check_waits_while_plugins_are_disabled(self):
        self.app.search_plugins_enabled = False
        with patch.object(gui.threading, "Thread") as worker:
            self.app.check_now()
        worker.assert_not_called()
        self.assertFalse(self.app.checking)

    def test_manual_check_runs_even_if_plugins_looked_disabled(self):
        # The user may have just enabled a plugin; the shown state can be stale.
        self.app.search_plugins_enabled = False
        with patch.object(gui.threading, "Thread") as worker:
            self.app.check_now(manual=True)
        worker.assert_called_once()
        self.assertTrue(self.app.checking)

    def test_check_stopped_for_missing_plugins_is_reported(self):
        self.app.qb_online = True
        self.app.search_plugins_enabled = True
        self.app.checking = True
        with patch.object(gui.messagebox, "showinfo") as help_message:
            self.app.check_finished("", no_plugins=True, manual=False)
            help_message.assert_not_called()  # automatic: notice only, no pop-up
            self.assertEqual(self.app.plugin_notice.winfo_manager(), "pack")
            self.assertIn("waiting for an enabled search plugin", self.app.status.get())
            self.app.check_finished("", no_plugins=True, manual=True)
        help_message.assert_called_once_with("Enable search plugins", gui.SEARCH_PLUGIN_HELP, parent=self.app)

    def test_unreadable_plugin_list_keeps_disabled_state_and_starts_no_check(self):
        self.app.last_check = time.time()
        with patch.object(self.app, "check_now") as check, patch.object(self.app, "show_window"):
            self.app.qbittorrent_seen(True, plugins_enabled=False)
            self.app.qbittorrent_seen(True, plugins_enabled=None)  # probe timed out
            check.assert_not_called()
            self.assertEqual(self.app.plugin_notice.winfo_manager(), "pack")
            self.assertFalse(self.app.search_plugins_enabled)
            self.app.qbittorrent_seen(True, plugins_enabled=True)
        check.assert_called_once()

    def test_unknown_plugin_status_does_not_show_disabled_notice(self):
        self.app.qb_online = True
        self.app.last_check = time.time()
        with patch.object(self.app, "check_now") as check:
            self.app.qbittorrent_seen(True, plugins_enabled=None)
        self.assertEqual(self.app.plugin_notice.winfo_manager(), "")
        self.assertIn("qBittorrent is running", self.app.status.get())
        check.assert_not_called()


class WindowCommandTests(unittest.TestCase):
    """Verify GUI commands even on runners without a working Tcl display."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="watcher-command-tests-")
        self.addCleanup(self.temp.cleanup)
        self.store = ConfigStore(Path(self.temp.name) / "series.json")
        self.series_id = self.store.add_series(dict(name="Example Show", season=1, next_episode=1))

    def test_edit_command_preserves_progress_from_background_check(self):
        old = self.store.current_entry(self.series_id)
        result = dict(old, quality="2160p")
        def wait(dialog):
            self.store.record_download(old, "episode one", "one")
        app = SimpleNamespace(store=self.store, selected=lambda: old, wait_window=wait,
                              refresh=Mock(), movies_shown=lambda: False)
        app.change = lambda command: gui.App.change(app, command)
        with patch.object(gui, "SeriesDialog", return_value=SimpleNamespace(result=result)):
            gui.App.edit(app)
        self.assertEqual(self.store.current_entry(self.series_id)["next_episode"], 2)
        self.assertEqual(self.store.current_entry(self.series_id)["quality"], "2160p")
        app.refresh.assert_called_once_with(force=True)

    def test_history_display_uses_each_records_season(self):
        first = self.store.current_entry(self.series_id)
        self.store.record_download(first, "season one", "one")
        self.store.edit_series(self.series_id, dict(season=2, next_episode=1))
        second = self.store.current_entry(self.series_id)
        self.store.record_download(second, "season two", "two")
        history = Mock()
        history.get_children.return_value = ()
        app = SimpleNamespace(selected=lambda: second, downloads=self.store.load()["downloads"],
                              history=history, edit_button=Mock(), pause_button=Mock(), remove_button=Mock(),
                              add_button=Mock(), movies_shown=lambda: False)
        gui.App.selection_changed(app)
        labels = {call.kwargs["values"][0] for call in history.insert.call_args_list}
        self.assertEqual(labels, {"S01E01", "S02E01"})

    def test_probe_storage_error_preserves_online_state_and_visibility(self):
        app = SimpleNamespace(config_retry=False, config_error=None, qb_online=True,
                              checking=False, status=Mock(), withdraw=Mock(), check_now=Mock())
        app.show_status = lambda: gui.App.show_status(app)
        error = "Could not read series.json: season must be positive"
        gui.App.qbittorrent_seen(app, None, error)
        self.assertTrue(app.qb_online)
        self.assertTrue(app.config_retry)
        app.status.set.assert_called_with(error)
        app.withdraw.assert_not_called()
        gui.App.qbittorrent_seen(app, False)
        self.assertTrue(app.qb_online)
        app.status.set.assert_called_with(error)
