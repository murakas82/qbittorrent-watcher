"""Cases from the fourth and fifth reviews: packs, magnets, late adds, startup, movies."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import engine as w
from matching import (magnet_ids, pick_candidates, pick_movie_candidates,
                      torrent_matches_ids)
from store import ConfigStore
from test_movies import FakeQB, row

V1 = "ab" * 20
V2 = "cd" * 32


class SeasonPackTests(unittest.TestCase):
    show = dict(name="Example Show", season=1, next_episode=1, quality="1080p")

    def picks(self, name):
        return bool(pick_candidates([row(name)], self.show))

    def test_ranges_and_multi_episode_releases_are_rejected(self):
        for name in ("Example.Show.S01E01-E10.Complete.1080p",
                     "Example.Show.S01E01-S01E10.Complete.1080p",
                     "Example.Show.S01E01E02.1080p",
                     "Example Show 1x01-1x10 1080p"):
            self.assertFalse(self.picks(name), name)

    def test_single_episodes_still_match(self):
        for name in ("Example.Show.S01E01.1080p.WEB",
                     "Example.Show.S01E01.Episode.Name.1080p",
                     "Example Show 1x01 1080p",
                     "Example.Show.S01E01.1080p.E-AC3"):
            self.assertTrue(self.picks(name), name)


class MagnetTests(unittest.TestCase):
    def test_every_magnet_form_is_read(self):
        import base64
        b32 = base64.b32encode(bytes.fromhex(V1)).decode()
        self.assertEqual(magnet_ids("magnet:?xt=urn:btih:" + V1.upper()), {V1})
        self.assertEqual(magnet_ids("magnet:?xt=urn:btih:" + b32), {V1})
        self.assertEqual(magnet_ids("magnet:?xt=urn%3Abtih%3A" + V1 + "&dn=x"), {V1})
        self.assertEqual(magnet_ids("magnet:?xt=urn:btmh:1220" + V2), {V2})
        self.assertEqual(magnet_ids("magnet:?xt=urn:btih:" + V1 + "&xt=urn:btmh:1220" + V2), {V1, V2})
        self.assertEqual(magnet_ids("magnet:?xt=0"), set())
        self.assertEqual(magnet_ids("https://example.test/page"), set())

    def test_v2_torrent_is_recognised_by_its_shortened_id(self):
        self.assertTrue(torrent_matches_ids({"hash": V2[:40]}, {V2}))
        self.assertTrue(torrent_matches_ids({"hash": "x", "infohash_v2": V2}, {V2}))
        self.assertFalse(torrent_matches_ids({"hash": "ef" * 20}, {V1, V2}))


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="watcher-review-tests-")
        self.addCleanup(self.temp.cleanup)
        self.store = ConfigStore(Path(self.temp.name) / "series.json")
        self.qb = FakeQB()
        self.deleted_tags = []
        original_call = self.qb.call
        def call(endpoint, params=None, **kwargs):
            if endpoint == "torrents/deleteTags":
                self.deleted_tags.append(params["tags"])
            return original_call(endpoint, params, **kwargs)
        self.qb.call = call
        self.log = []
        self.engine = w.WatcherEngine(self.store, client_factory=lambda url: self.qb,
                                      logger=self.log.append, max_episodes=1)
        patcher = patch.object(w.time, "sleep", lambda seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def downloads(self):
        return self.store.load()["downloads"]


class UnreadableMagnetTests(EngineCase):
    def test_unreadable_magnet_is_skipped_and_the_next_release_added(self):
        self.store.add_series(dict(name="Example Show", season=1, next_episode=1, quality="1080p"))
        self.qb.results = [row("Example.Show.S01E01.1080p.A", 90, url="magnet:?xt=urn:btih:"),
                           row("Example.Show.S01E01.1080p.B", 50)]
        self.engine.check()
        self.assertEqual([name for name, _ in self.qb.added], ["Example.Show.S01E01.1080p.B"])
        self.assertTrue(any("without a readable torrent ID" in line for line in self.log))


class LateAddTests(EngineCase):
    def test_late_episode_with_the_watcher_tag_is_claimed_for_cleanup(self):
        self.store.add_series(dict(name="Example Show", season=1, next_episode=1,
                                   remove_when_done=True))
        # Submitted by an earlier check, but it only appeared after the wait.
        self.qb.client = [dict(hash=V1, name="Example.Show.S01E01.1080p", tags="watcher-123",
                               state="stalledUP", progress=1.0)]
        self.engine.check()
        [record] = self.downloads()
        self.assertTrue(record["managed"])
        self.assertEqual(record["hash"], V1)
        self.assertEqual(self.deleted_tags, ["watcher-123"])
        self.engine.cleanup(qb=self.qb)
        self.assertEqual(self.qb.deleted, [V1])

    def test_untagged_torrent_is_still_only_observed(self):
        self.store.add_series(dict(name="Example Show", season=1, next_episode=1,
                                   remove_when_done=True))
        self.qb.client = [dict(hash=V1, name="Example.Show.S01E01.1080p", tags="",
                               state="stalledUP", progress=1.0)]
        self.engine.check()
        self.assertFalse(self.downloads()[0]["managed"])
        self.engine.cleanup(qb=self.qb)
        self.assertEqual(self.qb.deleted, [])

    def test_late_movie_with_the_watcher_tag_is_claimed(self):
        movie_id = self.store.add_movie(dict(name="Example Movie", year=2025))
        self.qb.client = [dict(hash=V1, name="Example.Movie.2025.1080p", tags="other, watcher-9",
                               state="downloading", progress=0.5)]
        self.engine.check()
        self.assertTrue(self.store.current_movie(movie_id)["done"])
        self.assertTrue(self.downloads()[0]["managed"])
        self.assertEqual(self.deleted_tags, ["watcher-9"])


class MovieFolderChangeTests(EngineCase):
    def test_folder_changed_during_the_search_is_used(self):
        self.store.set_setting("movie_folder", r"C:\Old")
        self.store.add_movie(dict(name="Example Movie", year=2025))
        results = [row("Example.Movie.2025.1080p.WEB")]
        def search(pattern, timeout):
            self.store.set_setting("movie_folder", r"C:\New")  # changed meanwhile
            return results
        self.qb.search = search
        self.engine.check()
        self.assertEqual(self.qb.added, [("Example.Movie.2025.1080p.WEB", r"C:\New\Example Movie (2025)")])


class MovieStoreRaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="watcher-review-store-")
        self.addCleanup(self.temp.cleanup)
        self.store = ConfigStore(Path(self.temp.name) / "series.json")
        self.movie_id = self.store.add_movie(dict(name="Example Movie", year=2025))

    def test_edit_during_add_keeps_the_new_movie_wanted(self):
        during = self.store.current_movie(self.movie_id)
        self.store.edit_movie(self.movie_id, dict(name="Other Movie", year=2024))
        self.store.record_movie(during, "Example.Movie.2025", V1)
        self.assertFalse(self.store.current_movie(self.movie_id)["done"])
        self.assertEqual(len(self.store.load()["downloads"]), 1)  # the add is still on record

    def test_unchanged_edit_still_marks_done(self):
        self.store.record_movie(self.store.current_movie(self.movie_id), "Example.Movie.2025", V1)
        self.assertTrue(self.store.current_movie(self.movie_id)["done"])

    def test_dialog_opened_before_the_download_does_not_undo_it(self):
        opened = self.store.current_movie(self.movie_id)
        self.store.record_movie(opened, "Example.Movie.2025", V1)  # a check finishes meanwhile
        self.store.edit_movie(self.movie_id, dict(opened, quality="720p"), original=opened)
        movie = self.store.current_movie(self.movie_id)
        self.assertTrue(movie["done"])
        self.assertEqual(movie["quality"], "720p")

    def test_unticking_downloaded_in_the_dialog_still_works(self):
        self.store.record_movie(self.store.current_movie(self.movie_id), "x", V1)
        opened = self.store.current_movie(self.movie_id)
        self.store.edit_movie(self.movie_id, dict(opened, done=False), original=opened)
        self.assertFalse(self.store.current_movie(self.movie_id)["done"])


class MovieMatchingEdgeTests(unittest.TestCase):
    movie = dict(name="Example Movie", year=2025, quality="2160p", min_seeders=1, must_not_contain=[])

    def picks(self, release, **changes):
        return bool(pick_movie_candidates([row(release)], dict(self.movie, **changes)))

    def test_stated_resolution_wins_over_uhd_label(self):
        self.assertFalse(self.picks("Example.Movie.2025.1080p.UHD.BluRay"))
        self.assertTrue(self.picks("Example.Movie.2025.UHD.BluRay"))
        self.assertTrue(self.picks("Example.Movie.2025.2160p.UHD.BluRay"))
        self.assertTrue(self.picks("Example.Movie.2025.1080p.UHD.BluRay", quality="1080p"))

    def test_remaster_year_is_not_the_movie_year(self):
        self.assertFalse(self.picks("Example.Movie.1984.2025.Remastered.1080p", quality="1080p"))
        self.assertTrue(self.picks("Example.Movie.1984.2025.Remastered.1080p", quality="1080p", year=1984))
        self.assertTrue(self.picks("1917.2019.1080p", name="1917", year=2019, quality="1080p"))


class StartupTests(unittest.TestCase):
    def test_failed_read_at_startup_leaves_windows_startup_alone(self):
        import tkinter as tk
        import importlib.machinery
        import importlib.util
        source = Path(__file__).resolve().parents[1] / "watcher_gui.pyw"
        loader = importlib.machinery.SourceFileLoader("watcher_gui_startup", str(source))
        gui = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(gui)
        temp = tempfile.TemporaryDirectory(prefix="watcher-startup-")
        self.addCleanup(temp.cleanup)
        store = ConfigStore(Path(temp.name) / "series.json")
        store.set_setting("autostart", False)
        try:
            app = gui.App(background=True, standalone=False, store=store)
        except tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        self.addCleanup(app.destroy)
        with patch.object(store, "load", side_effect=OSError("busy")), \
             patch.object(store, "set_setting") as saved, \
             patch.object(gui, "set_autostart") as registry, \
             patch.object(gui, "autostart_registered", return_value=False), \
             patch.object(gui.threading, "Thread"), patch.object(gui.socket, "socket"):
            app.start_background_work()
        registry.assert_not_called()
        saved.assert_not_called()
        self.assertFalse(app.autostart.get())


if __name__ == "__main__":
    unittest.main()
