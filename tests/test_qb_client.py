"""Exercise plugin submissions and their confirmation through the real client."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from engine import WatcherEngine
from qb_client import NoSearchPluginsError, QBittorrent
from store import ConfigStore


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.qb = QBittorrent("http://localhost:8080")
        self.row = dict(fileUrl="https://example.test/page", engineName="example",
                        fileName="Example.Show.S01E01.1080p", nbSeeders=10)

    def test_search_without_enabled_plugins_reports_setup_steps_before_starting(self):
        for plugins in ([], [dict(name="example", enabled=False)]):
            with self.subTest(plugins=plugins):
                self.qb.call = Mock(return_value=json.dumps(plugins))
                with self.assertRaises(NoSearchPluginsError) as raised:
                    self.qb.search("Example Show S01E01", 90)
                message = str(raised.exception)
                self.assertIn("No search plugins are enabled", message)
                self.assertIn("View > Search Engine", message)
                self.assertIn("Install a new one", message)
                self.qb.call.assert_called_once_with("search/plugins", timeout=3)

    def test_search_runs_when_at_least_one_plugin_is_enabled(self):
        plugins = [dict(name="disabled", enabled=False), dict(name="enabled", enabled=True)]
        self.qb.call = Mock(side_effect=[json.dumps(plugins), '{"id":1}',
                                        '[{"status":"Stopped"}]', '{"results":[]}', ""])
        self.assertEqual(self.qb.search("Example Show S01E01", 90), [])
        self.assertEqual([call.args[0] for call in self.qb.call.call_args_list],
                         ["search/plugins", "search/start", "search/status", "search/results", "search/delete"])

    def test_invalid_plugin_list_is_not_reported_as_no_enabled_plugins(self):
        for response in ('{"error":"unavailable"}', '[{"name":"example"}]',
                         '[{"enabled":"false"}]', "not JSON"):
            with self.subTest(response=response):
                self.qb.call = Mock(return_value=response)
                with self.assertRaises(ValueError) as raised:
                    self.qb.search("Example Show S01E01", 90)
                self.assertNotIsInstance(raised.exception, NoSearchPluginsError)
                self.assertEqual(self.qb.call.call_count, 1)

    def test_plugin_add_carries_folder_and_unique_tag_through_the_plugin(self):
        self.qb.call = Mock(side_effect=["2.13.1", "", ""])
        first = self.qb.add(self.row, "folder with spaces")
        second = self.qb.add(self.row, "folder with spaces")
        self.assertNotEqual(first, second)
        calls = self.qb.call.call_args_list
        self.assertEqual(calls[0].args, ("app/webapiVersion",))
        for call, tag in zip(calls[1:], (first, second)):
            self.assertEqual(call.args, ("torrents/add", dict(
                urls=self.row["fileUrl"], downloader="example", tags=tag,
                savepath="folder with spaces", autoTMM="false")))
            self.assertTrue(call.kwargs["post"])

    def test_old_client_reports_upgrade_before_submitting_a_page_link(self):
        self.qb.call = Mock(return_value="2.13.0")
        with self.assertRaisesRegex(ValueError, "Please update qBittorrent"):
            self.qb.add(self.row, "folder")
        self.qb.call.assert_called_once_with("app/webapiVersion")

    def test_missing_plugin_name_is_reported_before_submitting(self):
        self.qb.call = Mock()
        del self.row["engineName"]
        with self.assertRaisesRegex(ValueError, "no search plugin name"):
            self.qb.add(self.row, "folder")
        self.qb.call.assert_not_called()

    def test_magnet_add_remains_compatible_with_older_clients(self):
        self.qb.call = Mock(return_value="Ok.")
        row = dict(fileUrl="magnet:?xt=urn:btih:" + "a" * 40)
        self.assertIsNone(self.qb.add(row, ""))
        self.qb.call.assert_called_once_with("torrents/add", {"urls": row["fileUrl"]}, post=True)

    def test_real_client_and_engine_complete_page_link_lifecycle(self):
        """Only fake API responses; use real add, confirmation, move and cleanup."""
        client = self.qb
        requests = []
        torrents = []
        pending = None
        manual = dict(hash="manual", name=self.row["fileName"], progress=1, state="stalledUP")
        polls = 0

        def api(endpoint, params=None, **kwargs):
            nonlocal pending, polls
            requests.append((endpoint, params))
            if endpoint == "app/version":
                return "v5.3.0"
            if endpoint == "app/webapiVersion":
                return "2.16.2"
            if endpoint == "search/plugins":
                return '[{"name":"example","enabled":true}]'
            if endpoint == "search/start":
                return '{"id":1}'
            if endpoint == "search/status":
                return '[{"status":"Stopped"}]'
            if endpoint == "search/results":
                return json.dumps(dict(results=[self.row]))
            if endpoint == "torrents/add":
                self.assertEqual(params["downloader"], "example")
                self.assertEqual(params["savepath"], "series-folder")
                torrents.append(manual)
                pending = dict(hash="requested", name="Example.Show.S01E01.actual-name",
                               tags="personal, " + params["tags"], progress=0.5, state="downloading")
                return '{"pending_count":1,"added_torrent_ids":[]}'
            if endpoint == "torrents/info":
                if pending:
                    polls += 1
                    if polls == 2:
                        torrents.append(pending)
                        pending = None
                return json.dumps(torrents)
            if endpoint == "torrents/deleteTags":
                for torrent in torrents:
                    torrent["tags"] = ", ".join(tag.strip() for tag in torrent.get("tags", "").split(",")
                                              if tag.strip() and tag.strip() != params["tags"])
            return ""

        client.call = api
        with tempfile.TemporaryDirectory(prefix="watcher-client-tests-") as directory:
            store = ConfigStore(Path(directory) / "series.json")
            series_id = store.add_series(dict(name="Example Show", season=1, next_episode=1,
                                              save_path="series-folder", remove_when_done=True))
            engine = WatcherEngine(store, lambda url: client, logger=lambda message: None, max_episodes=1)
            with patch("engine.time.sleep"):
                self.assertEqual(engine.check(), 0)
            records = store.load()["downloads"]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["hash"], "requested")
            self.assertTrue(records[0]["managed"])
            self.assertEqual(store.current_entry(series_id)["next_episode"], 2)
            self.assertIn(("torrents/setLocation", dict(hashes="requested", location="series-folder")), requests)
            self.assertEqual(torrents[1]["tags"], "personal")
            torrents[1].update(progress=1, state="stalledUP")
            engine.cleanup(qb=client)
            deletes = [params for endpoint, params in requests if endpoint == "torrents/delete"]
            self.assertEqual(deletes, [dict(hashes="requested", deleteFiles="false")])
            self.assertEqual(store.load()["downloads"][0]["status"], "removed")


if __name__ == "__main__":
    unittest.main()
