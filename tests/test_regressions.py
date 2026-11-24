"""Regression cases from the project reviews; no live qBittorrent required."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import engine as w
from matching import name_matches
from qb_client import NoSearchPluginsError, SEARCH_PLUGIN_HELP
from store import ConfigStore


class FakeQB:
    def __init__(self):
        self.added = []
        self.deleted = []
        self.client = []
        self.search_plugins_enabled = True
        self.plugin_probes = []

    def call(self, endpoint, params=None, **kwargs):
        if endpoint == "torrents/delete":
            self.deleted.append(params["hashes"])
        return "v5.0"

    def torrents(self):
        return list(self.client)

    def has_enabled_search_plugins(self, timeout=30):
        self.plugin_probes.append(timeout)
        return self.search_plugins_enabled

    def search(self, pattern, timeout):
        return [dict(fileName=pattern + ".1080p", fileUrl="magnet:?xt=urn:btih:" + f"{i:040x}",
                     nbSeeders=10 - i) for i in range(3)]

    def add(self, row, path):
        self.added.append((row, path))

    def move(self, *args):
        pass


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="watcher-tests-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = ConfigStore(root / "series.json")
        fields = dict(name="Example Show", season=1, next_episode=1,
                      quality="1080p", min_seeders=1, save_path="folder-A",
                      enabled=True, remove_when_done=True)
        series_id = self.store.add_series(fields)
        self.show = self.store.current_entry(series_id)
        self.qb = FakeQB()
        self.engine = w.WatcherEngine(self.store, client_factory=lambda url: self.qb,
                                      logger=lambda message: None, max_episodes=1)

    def edit(self, **changes):
        self.store.edit_series(self.show["id"], changes)

    def run_check(self, wait=None):
        if wait:
            with patch.object(w, "wait_for_added", wait):
                self.engine.check()
        else:
            self.engine.check()

    def test_unrelated_title_rejected(self):
        self.assertFalse(name_matches(dict(self.show, name="You"), "Young Sheldon"))
        self.assertTrue(name_matches(self.show, "ExampleShow"))

    def test_pause_prevents_retry(self):
        def wait(*args):
            self.edit(enabled=False)
            return None, False
        self.run_check(wait)
        self.assertEqual(len(self.qb.added), 1)

    def test_settings_edit_invalidates_search(self):
        search = self.qb.search
        def edited_search(*args):
            rows = search(*args)
            self.edit(quality="2160p", min_seeders=100, save_path="folder-B")
            return rows
        self.qb.search = edited_search
        self.run_check()
        self.assertEqual(self.qb.added, [])

    def test_explicit_rewind_survives_progress(self):
        self.edit(next_episode=5)
        def wait(*args):
            self.edit(next_episode=1)
            return dict(name="Example.Show.S01E05.1080p", hash="owned"), True
        self.run_check(wait)
        self.assertEqual(self.store.load()["series"][0]["next_episode"], 1)

    def test_redownload_tracks_replacement(self):
        self.store.record_download(self.show, "old", "old")
        self.edit(next_episode=1)
        self.run_check(lambda *args: (dict(name="Example.Show.S01E01.1080p", hash="new"), True))
        self.assertEqual({d["hash"] for d in self.store.load()["downloads"]}, {"old", "new"})

    def test_manual_add_during_search_is_not_owned(self):
        search = self.qb.search
        def manual_search(*args):
            self.qb.client.append(dict(name="Example.Show.S01E01.1080p", hash="manual"))
            return search(*args)
        self.qb.search = manual_search
        self.run_check()
        self.assertEqual(self.qb.added, [])
        self.assertFalse(self.store.load()["downloads"][0]["managed"])
        self.assertEqual(self.store.load()["downloads"][0]["hash"], "manual")

    def test_cleanup_does_not_remove_manual_torrent(self):
        self.qb.client = [dict(name="Example.Show.S01E01.1080p", hash="manual",
                               progress=1, state="stalledUP")]
        self.store.record_download(self.show, "owned", "ours")
        self.engine.cleanup(qb=self.qb)
        self.assertEqual(self.qb.deleted, [])

    def test_old_season_is_still_cleaned_and_kept_in_history(self):
        self.store.record_download(self.show, "owned", "ours")
        self.edit(season=2, next_episode=1)
        self.qb.client = [dict(hash="ours", progress=1, state="stalledUP")]
        self.engine.cleanup(qb=self.qb)
        self.assertEqual(self.qb.deleted, ["ours"])
        record = self.store.load()["downloads"][0]
        self.assertEqual(record["season"], 1)
        self.assertEqual(record["status"], "removed")
        self.engine.cleanup(qb=self.qb)
        self.assertEqual(self.qb.deleted, ["ours"])

    def test_both_replacement_and_original_remain_managed(self):
        self.store.record_download(self.show, "old", "old")
        self.edit(next_episode=1)
        self.store.record_download(self.show, "new", "new")
        self.qb.client = [dict(hash=h, progress=1, state="stalledUP") for h in ("old", "new")]
        self.engine.cleanup(qb=self.qb)
        self.assertEqual(set(self.qb.deleted), {"old", "new"})

    def test_rename_during_add_preserves_ownership(self):
        def wait(*args):
            self.edit(name="Renamed Show")
            return dict(name="Example.Show.S01E01.1080p", hash="ours"), True
        self.run_check(wait)
        record = self.store.load()["downloads"][0]
        self.assertTrue(record["managed"])
        self.assertEqual(record["series_id"], self.show["id"])
        self.qb.client = [dict(hash="ours", progress=1, state="stalledUP")]
        self.engine.cleanup(qb=self.qb)
        self.assertEqual(self.qb.deleted, ["ours"])

    def test_dry_run_does_not_add_remove_or_change_records(self):
        self.store.record_download(self.show, "owned", "ours")
        self.edit(next_episode=1)
        self.qb.client = [dict(name="Example.Show.S01E01.1080p", hash="ours", progress=1, state="stalledUP")]
        before = self.store.path.read_bytes()
        self.engine.check(dry_run=True)
        self.assertEqual(self.qb.added, [])
        self.assertEqual(self.qb.deleted, [])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_poll_cleans_up_without_taking_the_check_lock(self):
        self.store.record_download(self.show, "owned", "ours")
        self.qb.client = [dict(hash="ours", progress=1, state="stalledUP")]
        lock = w.FileLock(self.store.path.parent / "watcher.lock")
        self.assertTrue(lock.acquire())
        try:
            self.assertTrue(self.engine.poll(tidy=True))
        finally:
            lock.release()
        self.assertEqual(self.qb.deleted, ["ours"])

    def test_no_enabled_plugins_stops_searches_with_setup_instructions(self):
        before = self.store.path.read_bytes()
        self.engine.logger = Mock()
        self.qb.search_plugins_enabled = False
        with patch.object(self.qb, "search") as search:
            self.assertEqual(self.engine.check(), w.NO_PLUGINS_EXIT)
        search.assert_not_called()
        self.engine.logger.assert_called_once_with(SEARCH_PLUGIN_HELP)
        self.assertEqual(self.qb.added, [])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_disabled_plugins_do_not_stop_connectivity_or_cleanup(self):
        self.qb.search_plugins_enabled = False
        self.store.record_download(self.show, "owned", "ours")
        self.qb.client = [dict(hash="ours", progress=1, state="stalledUP")]
        self.assertTrue(self.engine.poll(tidy=True))
        self.assertFalse(self.engine.search_plugins_enabled)
        self.assertEqual(self.qb.deleted, ["ours"])
        self.qb.search_plugins_enabled = True
        self.assertTrue(self.engine.poll())
        self.assertTrue(self.engine.search_plugins_enabled)

    def test_unavailable_plugin_list_does_not_mean_offline_or_disabled(self):
        self.engine.search_plugins_enabled = False
        for error in (OSError("unavailable"), ValueError("invalid plugin response")):
            with self.subTest(error=error), \
                 patch.object(self.qb, "has_enabled_search_plugins", side_effect=error):
                self.assertTrue(self.engine.poll())
                self.assertIsNone(self.engine.search_plugins_enabled)

    def test_exact_magnet_identity_wins_over_manual_match(self):
        torrent_hash = "a" * 40
        self.qb.client = [dict(name="Example.Show.S01E01.1080p", hash="manual"),
                          dict(name="Example.Show.S01E01.1080p", hash=torrent_hash)]
        row = dict(fileUrl="magnet:?xt=urn:btih:" + torrent_hash)
        torrent, owned = w.wait_for_added(self.qb, row, self.show, set())
        self.assertEqual(torrent["hash"], torrent_hash)
        self.assertTrue(owned)

    def test_ambiguous_plugin_download_is_not_claimed(self):
        self.qb.client = [dict(name="Example.Show.S01E01.1080p", hash=h) for h in ("one", "two")]
        row = dict(fileUrl="https://example.test/torrent", fileName="Example.Show.S01E01.1080p")
        with patch.object(w, "ADD_WAIT_SECONDS", 0):
            self.assertEqual(w.wait_for_added(self.qb, row, self.show, set(), "watcher-requested"),
                             (None, False))

    def test_magnet_waits_for_requested_hash_when_manual_release_appears_first(self):
        expected = dict(name="Example.Show.S01E01.1080p", hash="a" * 40)
        manual = dict(name="Example.Show.S01E01.720p.manual", hash="b" * 40)
        row = dict(fileUrl="magnet:?xt=urn:btih:" + expected["hash"])
        with patch.object(self.qb, "torrents", side_effect=[[manual], [manual, expected]]) as torrents, \
             patch.object(w.time, "sleep"):
            torrent, owned = w.wait_for_added(self.qb, row, self.show, set())
        self.assertEqual(torrents.call_count, 2)
        self.assertEqual(torrent["hash"], expected["hash"])
        self.assertTrue(owned)

    def test_magnet_times_out_without_accepting_another_release(self):
        self.qb.client = [dict(name="Example.Show.S01E01.1080p.manual", hash="b" * 40)]
        row = dict(fileUrl="magnet:?xt=urn:btih:" + "a" * 40)
        with patch.object(w, "ADD_WAIT_SECONDS", 0):
            self.assertEqual(w.wait_for_added(self.qb, row, self.show, set()), (None, False))

    def test_existing_magnet_hash_is_not_claimed(self):
        self.qb.client = [dict(name="Example.Show.S01E01.1080p", hash="a" * 40)]
        row = dict(fileUrl="magnet:?xt=urn:btih:" + "a" * 40)
        torrent, owned = w.wait_for_added(self.qb, row, self.show, {"A" * 40})
        self.assertIsNotNone(torrent)
        self.assertFalse(owned)

    def test_plugin_confirms_changed_name_and_manages_only_tagged_download(self):
        tag = "watcher-requested"
        manual = dict(name="Example.Show.S01E01.1080p.search-title", hash="manual")
        requested = dict(name="Example.Show.S01E01.1080p.actual-name", hash="requested",
                         tags="personal, " + tag)
        row = dict(fileName=manual["name"], fileUrl="https://example.test/plugin", nbSeeders=10)
        self.qb.search = lambda pattern, timeout: [row]
        self.qb.move = Mock()
        snapshots = iter([[manual], [manual, requested]])
        submitted = False
        def torrents():
            if not submitted:
                return []
            self.qb.client = next(snapshots)
            return list(self.qb.client)
        def add(release, path):
            nonlocal submitted
            submitted = True
            self.qb.added.append((release, path))
            return tag
        self.qb.torrents = torrents
        self.qb.add = add
        with patch.object(w.time, "sleep"):
            self.run_check()
        self.qb.move.assert_called_once_with("requested", "folder-A")
        record = self.store.load()["downloads"][0]
        self.assertEqual(record["title"], requested["name"])
        self.assertTrue(record["managed"])
        self.assertEqual(record["hash"], "requested")
        self.assertEqual(self.store.current_entry(self.show["id"])["next_episode"], 2)
        self.qb.torrents = lambda: [dict(t, progress=1, state="stalledUP") for t in (manual, requested)]
        self.engine.cleanup(qb=self.qb)
        self.assertEqual(self.qb.deleted, ["requested"])

    def test_single_plugin_match_is_not_proof_of_ownership(self):
        row = dict(fileName="Example.Show.S01E01.1080p", fileUrl="https://example.test/plugin")
        self.qb.client = [dict(name=row["fileName"], hash="manual")]
        with patch.object(w, "ADD_WAIT_SECONDS", 0):
            self.assertEqual(w.wait_for_added(self.qb, row, self.show, set(), "watcher-requested"),
                             (None, False))

    def test_existing_tagged_torrent_is_not_claimed(self):
        row = dict(fileUrl="https://example.test/plugin")
        self.qb.client = [dict(name="changed name", hash="existing", tags="watcher-requested")]
        torrent, owned = w.wait_for_added(self.qb, row, self.show, {"EXISTING"}, "watcher-requested")
        self.assertEqual(torrent["hash"], "existing")
        self.assertFalse(owned)

    def test_plugin_confirmation_requires_the_exact_tag(self):
        row = dict(fileUrl="https://example.test/plugin")
        self.qb.client = [dict(name="Example.Show.S01E01.1080p", hash="manual",
                               tags="watcher-requested-other")]
        with patch.object(w, "ADD_WAIT_SECONDS", 0):
            self.assertEqual(w.wait_for_added(self.qb, row, self.show, set(), "watcher-requested"),
                             (None, False))

    def test_rewind_does_not_duplicate_or_unmanage_existing_torrent(self):
        self.store.record_download(self.show, "Example.Show.S01E01.1080p", "owned")
        original = self.store.load()["downloads"][0]
        self.edit(next_episode=1)
        self.qb.client = [dict(name=original["title"], hash="owned")]
        self.run_check()
        self.assertEqual(self.store.load()["downloads"], [original])
        self.assertEqual(self.store.current_entry(self.show["id"])["next_episode"], 2)
        self.assertEqual(self.qb.added, [])

    def test_poll_propagates_configuration_errors_without_probing_client(self):
        for error in (ValueError("season must be positive"), OSError("watch list busy")):
            with self.subTest(error=error), patch.object(self.store, "load", side_effect=error), \
                 patch.object(self.qb, "call") as call:
                with self.assertRaises(type(error)):
                    self.engine.poll(tidy=True)
                call.assert_not_called()

    def test_plugin_does_not_confirm_a_different_release(self):
        row = dict(fileName="Example.Show.S01E01.1080p", fileUrl="https://example.test/plugin")
        self.qb.client = [dict(name="Example.Show.S01E01.720p.manual", hash="manual")]
        with patch.object(w, "ADD_WAIT_SECONDS", 0):
            self.assertEqual(w.wait_for_added(self.qb, row, self.show, set()), (None, False))

    def test_full_add_confirmation_and_later_cleanup(self):
        torrent_hash = "a" * 40
        row = dict(fileName="Example.Show.S01E01.1080p", nbSeeders=10,
                   fileUrl="magnet:?xt=urn:btih:" + torrent_hash)
        self.qb.search = lambda pattern, timeout: [row] if "S01E01" in pattern else []
        def add(release, path):
            self.qb.added.append((release, path))
            self.qb.client.append(dict(name=release["fileName"], hash=torrent_hash,
                                       progress=0.1, state="downloading"))
        self.qb.add = add
        self.engine.max_episodes = 3
        self.engine.check()
        config = self.store.load()
        self.assertEqual(config["series"][0]["next_episode"], 2)
        self.assertEqual(config["downloads"][0]["hash"], torrent_hash)
        self.assertEqual(len(self.qb.added), 1)
        self.qb.client[0].update(progress=1, state="stalledUP")
        self.engine.cleanup(qb=self.qb)
        self.assertEqual(self.qb.deleted, [torrent_hash])
        self.assertEqual(self.store.load()["downloads"][0]["status"], "removed")

    def test_cleanup_failure_preserves_previous_successes(self):
        self.store.record_download(self.show, "one", "one")
        self.store.record_download(dict(self.show, next_episode=2), "two", "two")
        self.qb.client = [dict(hash=h, progress=1, state="stalledUP") for h in ("one", "two")]
        call = self.qb.call
        def disconnect(endpoint, params=None, **kwargs):
            if endpoint == "torrents/delete" and params["hashes"] == "two":
                raise OSError("qBittorrent closed")
            return call(endpoint, params, **kwargs)
        self.qb.call = disconnect
        self.engine.cleanup(qb=self.qb)
        states = {d["hash"]: d["status"] for d in self.store.load()["downloads"]}
        self.assertEqual(states, {"one": "removed", "two": "added"})


if __name__ == "__main__":
    unittest.main()


class PluginProbeRegressions(unittest.TestCase):
    """The plugin list is read once per check, patiently, and never decides alone."""

    setUp = ReviewRegressions.setUp

    def test_plugins_are_checked_once_per_check_with_the_search_timeout(self):
        self.engine.max_episodes = 5
        with patch.object(w, "wait_for_added", return_value=(None, False)):
            self.engine.check()
        self.assertEqual(self.qb.plugin_probes, [90])

    def test_unreadable_plugin_list_does_not_stop_searches(self):
        self.engine.logger = Mock()
        with patch.object(self.qb, "has_enabled_search_plugins", side_effect=OSError("timed out")),              patch.object(self.qb, "search", return_value=[]) as search:
            self.assertEqual(self.engine.check(), 0)
        search.assert_called()
        self.assertIn("searching anyway", self.engine.logger.call_args_list[0].args[0])

    def test_client_without_plugin_probe_does_not_break_polling(self):
        class OldClient:
            def call(self, endpoint, params=None, **kwargs):
                return "v5.0"

            def torrents(self):
                return []
        self.engine.client_factory = lambda url: OldClient()
        self.assertTrue(self.engine.poll())
        self.assertIsNone(self.engine.search_plugins_enabled)
