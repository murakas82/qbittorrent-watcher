"""Exercise the real Tk widgets with an isolated store and no background work."""

import importlib.machinery
import importlib.util
import tempfile
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
                              refresh=Mock())
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
                              history=history, edit_button=Mock(), pause_button=Mock(), remove_button=Mock())
        gui.App.selection_changed(app)
        labels = {call.kwargs["values"][0] for call in history.insert.call_args_list}
        self.assertEqual(labels, {"S01E01", "S02E01"})
