"""Movie watching: matching, storage, the check and cleanup. No live qBittorrent."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import engine as w
from matching import normalize, pick_candidates, pick_movie_candidates
from store import ConfigStore

HASH = "ab" * 20


def row(name, seeders=50, url=None):
    return dict(fileName=name, fileUrl=url or "magnet:?xt=urn:btih:" + HASH,
                nbSeeders=seeders, engineName="example")


class MovieMatchingTests(unittest.TestCase):
    movie = dict(name="Dune", year=2021, quality="2160p", min_seeders=1,
                 must_not_contain=["CAM", "TS", "HDTS"])

    def picks(self, name):
        return bool(pick_movie_candidates([row(name)], self.movie))

    def test_title_and_year_must_both_match(self):
        self.assertTrue(self.picks("Dune.2021.2160p.WEB-DL"))
        self.assertTrue(self.picks("Dune (2021) 2160p BluRay"))
        self.assertFalse(self.picks("Dune 1984 2160p BluRay"))  # the remake's original
        self.assertFalse(self.picks("Dune Part Two 2024 2160p"))  # the sequel
        self.assertFalse(self.picks("Dune 2160p WEB"))  # no year at all

    def test_4k_quality_accepts_every_way_releases_name_it(self):
        for name in ("Dune 2021 2160p WEB", "Dune 2021 4K HDR", "Dune 2021 UHD BluRay"):
            self.assertTrue(self.picks(name), name)
        self.assertFalse(self.picks("Dune 2021 1080p WEB"))

    def test_4k_also_works_for_series(self):
        show = dict(name="Example Show", season=1, next_episode=1, quality="2160p")
        self.assertTrue(pick_candidates([row("Example Show S01E01 4K WEB")], show))
        self.assertFalse(pick_candidates([row("Example Show S01E01 1080p WEB")], show))

    def test_cinema_recordings_are_skipped(self):
        self.assertFalse(self.picks("Dune 2021 2160p TS"))
        self.assertFalse(self.picks("Dune 2021 2160p HDTS x264"))

    def test_title_that_is_a_number(self):
        movie = dict(self.movie, name="1917", year=2019, quality="")
        self.assertTrue(pick_movie_candidates([row("1917 (2019) 1080p")], movie))
        self.assertFalse(pick_movie_candidates([row("1917 1080p")], movie))


class FakeQB:
    def __init__(self, results=()):
        self.results = list(results)
        self.client = []
        self.added = []
        self.moved = []
        self.deleted = []
        self.searches = []
        self.layouts = []

    def call(self, endpoint, params=None, **kwargs):
        if endpoint == "torrents/delete":
            self.deleted.append(params["hashes"])
            self.client = [t for t in self.client if t["hash"] != params["hashes"]]
        return "{}" if endpoint == "app/preferences" else "v5.3.0"

    def has_enabled_search_plugins(self, timeout=30):
        return True

    def search(self, pattern, timeout):
        self.searches.append(pattern)
        return self.results

    def torrents(self):
        return list(self.client)

    def add(self, result, folder, content_layout=None):
        self.added.append((result["fileName"], folder))
        self.layouts.append(content_layout)
        self.client.append(dict(hash=HASH, name=result["fileName"], state="downloading",
                                progress=0.0, tags=""))
        return None

    def move(self, torrent_hash, folder):
        self.moved.append((torrent_hash, folder))


class MovieCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="watcher-movie-tests-")
        self.addCleanup(self.temp.cleanup)
        self.store = ConfigStore(Path(self.temp.name) / "series.json")
        self.store.set_setting("movie_folder", r"C:\Media\Filmid")
        self.movie_id = self.store.add_movie(dict(name="Example Movie", year=2025, quality="2160p"))
        self.qb = FakeQB([row("Example.Movie.2025.2160p.WEB-DL")])
        self.log = []
        self.engine = w.WatcherEngine(self.store, client_factory=lambda url: self.qb,
                                      logger=self.log.append)
        patcher = patch.object(w.time, "sleep", lambda seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def movie(self):
        return self.store.current_movie(self.movie_id)

    def records(self):
        return [d for d in self.store.load()["downloads"] if d.get("movie_id") == self.movie_id]

    def test_new_movie_gets_sensible_defaults(self):
        movie = self.movie()
        self.assertIn("TS", movie["must_not_contain"])
        self.assertIn("CAM", movie["must_not_contain"])
        self.assertEqual(movie["min_seeders"], 5)
        self.assertTrue(movie["remove_when_done"])
        self.assertFalse(movie["done"])

    def test_found_movie_gets_its_own_folder_and_is_marked_done(self):
        self.assertEqual(self.engine.check(), 0)
        folder = r"C:\Media\Filmid\Example Movie (2025)"
        self.assertEqual(self.qb.added, [("Example.Movie.2025.2160p.WEB-DL", folder)])
        # The torrent's own top folder is dropped: files go straight into the
        # movie's folder, whether it holds one video file or several files.
        self.assertEqual(self.qb.layouts, ["NoSubfolder"])
        self.assertEqual(self.qb.moved, [(HASH, folder)])
        self.assertTrue(self.movie()["done"])
        [record] = self.records()
        self.assertTrue(record["managed"])
        self.assertEqual(record["hash"], HASH)

    def test_own_folder_overrides_the_movie_folder(self):
        self.store.edit_movie(self.movie_id, dict(save_path=r"D:\Elsewhere"))
        self.engine.check()
        self.assertEqual(self.qb.added[0][1], r"D:\Elsewhere")
        self.assertEqual(self.qb.layouts, [None])  # used exactly as given

    def test_folder_name_drops_characters_windows_does_not_allow(self):
        self.assertEqual(w.movie_folder_name(dict(name="Mission: Impossible", year=2023)),
                         "Mission Impossible (2023)")
        self.assertEqual(w.movie_folder_name(dict(name="What If...?", year=2021)), "What If (2021)")

    def test_without_a_movie_folder_qbittorrent_decides(self):
        self.store.set_setting("movie_folder", "")
        self.engine.check()
        self.assertEqual(self.qb.added[0][1], "")
        self.assertEqual(self.qb.layouts, [None])

    def test_done_movie_is_not_searched_again(self):
        self.engine.check()
        self.qb.searches.clear()
        self.engine.check()
        self.assertEqual(self.qb.searches, [])
        self.assertEqual(len(self.qb.added), 1)

    def test_finished_movie_is_removed_but_files_kept(self):
        self.engine.check()
        self.qb.client[0].update(state="stalledUP", progress=1.0)
        self.engine.cleanup(qb=self.qb)
        self.assertEqual(self.qb.deleted, [HASH])
        self.assertEqual(self.records()[0]["status"], "removed")

    def test_movie_already_in_qbittorrent_is_marked_done_but_never_removed(self):
        self.qb.client = [dict(hash="cd" * 20, name="Example Movie 2025 1080p", state="stalledUP",
                               progress=1.0, tags="")]
        self.engine.check()
        self.assertEqual(self.qb.added, [])
        self.assertTrue(self.movie()["done"])
        self.assertFalse(self.records()[0]["managed"])
        self.engine.cleanup(qb=self.qb)
        self.assertEqual(self.qb.deleted, [])

    def test_not_found_movie_keeps_waiting(self):
        self.qb.results = [row("Example Movie 2025 1080p WEB")]  # wrong quality
        self.engine.check()
        self.assertFalse(self.movie()["done"])
        self.assertEqual(self.qb.added, [])
        self.assertIn("not found yet", self.log[-1])

    def test_paused_movie_is_not_searched(self):
        self.store.toggle_movie(self.movie_id)
        self.engine.check()
        self.assertEqual(self.qb.searches, [])

    def test_dry_run_changes_nothing(self):
        before = self.store.path.read_bytes()
        self.engine.check(dry_run=True)
        self.assertEqual(self.qb.added, [])
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertIn("would add", self.log[-1])

    def test_unticking_done_searches_again(self):
        self.engine.check()
        self.store.edit_movie(self.movie_id, dict(done=False))
        self.qb.client.clear()
        self.qb.searches.clear()
        self.engine.check()
        self.assertTrue(self.qb.searches)

    def test_removed_movie_is_not_brought_back_by_a_late_record(self):
        movie = self.movie()
        self.store.remove_movie(self.movie_id)
        self.store.record_movie(movie, "late", HASH)
        self.assertEqual(self.store.load()["movies"], [])
        self.assertEqual(self.store.load()["downloads"], [])


class MovieStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="watcher-movie-store-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "series.json"
        self.store = ConfigStore(self.path)

    def test_version_2_list_is_upgraded_with_a_backup(self):
        old = {"schema_version": 2, "qbittorrent_url": "http://127.0.0.1:8080",
               "search_timeout_seconds": 90, "max_downloads": 2,
               "series": [dict(id="s1", name="Example Show", season=1, next_episode=4)],
               "downloads": [dict(id="d1", series_id="s1", season=1, episode=3, title="x",
                                  added="2026-11-01 10:00", managed=False, status="observed")]}
        original = json.dumps(old).encode("utf-8")
        self.path.write_bytes(original)
        config = self.store.load()
        self.assertEqual(config["schema_version"], 3)
        self.assertEqual(config["movies"], [])
        self.assertEqual(config["series"][0]["next_episode"], 4)
        self.assertEqual(len(config["downloads"]), 1)
        self.assertEqual(config["max_downloads"], 2)
        self.assertEqual((self.path.parent / "series.json.v2.bak").read_bytes(), original)

    def test_duplicate_movie_is_rejected_but_a_remake_is_not(self):
        self.store.add_movie(dict(name="Dune", year=2021))
        with self.assertRaisesRegex(ValueError, "already in the list"):
            self.store.add_movie(dict(name="dune", year=2021))
        self.store.add_movie(dict(name="Dune", year=1984))
        self.assertEqual(len(self.store.load()["movies"]), 2)

    def test_movie_needs_a_valid_year(self):
        with self.assertRaises(ValueError):
            self.store.add_movie(dict(name="Dune", year="2021"))
        with self.assertRaises(ValueError):
            self.store.add_movie(dict(name="Dune"))

    def test_movie_download_must_point_at_a_known_movie(self):
        movie_id = self.store.add_movie(dict(name="Dune", year=2021))
        def orphan(config):
            config["downloads"].append(dict(id="x", movie_id="missing", season=None, episode=None,
                                            title="", added="", managed=False, status="observed"))
        with self.assertRaisesRegex(ValueError, "unknown movie"):
            self.store.update(orphan)
        def both(config):
            config["downloads"].append(dict(id="y", movie_id=movie_id, series_id="s", season=None,
                                            episode=None, title="", added="", managed=False,
                                            status="observed"))
        with self.assertRaisesRegex(ValueError, "one series or one movie"):
            self.store.update(both)


if __name__ == "__main__":
    unittest.main()
