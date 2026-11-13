import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from store import ConfigStore, FileLock, SCHEMA_VERSION


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="watcher-store-tests-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "series.json"
        self.store = ConfigStore(self.path)
        self.fields = dict(name="Example Show", season=2, next_episode=4)

    def legacy(self):
        return dict(qbittorrent_url="http://localhost:8080", max_downloads=2,
                    autostart=False, series=[dict(self.fields, history=[
                        dict(episode=1, title="manual", added="2026-11-01 12:00"),
                        dict(episode=2, title="owned", hash="owned", added="2026-11-02 12:00")],
                        cleanup=[dict(hash="old-season", label="S01E08", since="2026-11-01")])])

    def test_migration_backs_up_exact_bytes_and_preserves_downloads(self):
        raw = (json.dumps(self.legacy(), indent=4) + "\n").encode("utf-8")
        self.path.write_bytes(raw)
        result = self.store.load()
        self.assertEqual(self.path.with_name("series.json.v1.bak").read_bytes(), raw)
        self.assertEqual(result["schema_version"], SCHEMA_VERSION)
        self.assertEqual(result["max_downloads"], 2)
        self.assertFalse(result["autostart"])
        self.assertNotIn("history", result["series"][0])
        self.assertNotIn("cleanup", result["series"][0])
        records = result["downloads"]
        self.assertEqual(len(records), 3)
        self.assertFalse(records[0]["managed"])
        self.assertEqual((records[2]["season"], records[2]["episode"]), (1, 8))
        self.assertTrue(records[2]["managed"])
        self.assertEqual(self.store.load(), result)
        self.assertEqual(len(list(self.path.parent.glob("*.bak"))), 1)

    def test_migration_does_not_overwrite_existing_backup(self):
        self.path.write_text(json.dumps(self.legacy()), encoding="utf-8")
        old_backup = self.path.with_name("series.json.v1.bak")
        old_backup.write_bytes(b"previous backup")
        self.store.load()
        self.assertEqual(old_backup.read_bytes(), b"previous backup")
        self.assertEqual(len(list(self.path.parent.glob("*.bak"))), 2)

    def test_future_or_invalid_schema_is_never_overwritten(self):
        for value in (dict(schema_version=99, series=[]), dict(series="invalid"), []):
            with self.subTest(value=value):
                raw = json.dumps(value).encode("utf-8")
                self.path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    self.store.load()
                self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(list(self.path.parent.glob("*.bak")), [])

    def test_failed_update_preserves_valid_file(self):
        series_id = self.store.add_series(self.fields)
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.store.edit_series(series_id, dict(next_episode=-1))
        self.assertEqual(self.path.read_bytes(), before)

    def test_ids_and_downloads_survive_rename_and_season_change(self):
        series_id = self.store.add_series(self.fields)
        original = self.store.current_entry(series_id)
        self.store.record_download(original, "owned", "hash")
        self.store.edit_series(series_id, dict(name="Renamed Show", season=3, next_episode=1))
        result = self.store.load()
        self.assertEqual(result["series"][0]["id"], series_id)
        self.assertEqual(result["downloads"][0]["series_id"], series_id)
        self.assertEqual((result["downloads"][0]["season"], result["downloads"][0]["episode"]), (2, 4))

    def test_unchanged_dialog_does_not_rewind_progress(self):
        series_id = self.store.add_series(self.fields)
        original = self.store.current_entry(series_id)
        self.store.record_download(original, "owned", "hash")
        edits = dict(original, quality="2160p")
        self.store.edit_series(series_id, edits, original=original)
        current = self.store.current_entry(series_id)
        self.assertEqual(current["next_episode"], 5)
        self.assertEqual(current["quality"], "2160p")

    def test_inflight_old_season_does_not_advance_new_season(self):
        series_id = self.store.add_series(self.fields)
        original = self.store.current_entry(series_id)
        self.store.edit_series(series_id, dict(season=3, next_episode=4))
        self.store.record_download(original, "old season", "hash")
        self.assertEqual(self.store.current_entry(series_id)["next_episode"], 4)
        self.assertEqual(self.store.load()["downloads"][0]["season"], 2)

    def test_remove_forgets_records_and_inflight_add_cannot_recreate_them(self):
        series_id = self.store.add_series(self.fields)
        original = self.store.current_entry(series_id)
        self.store.record_download(original, "owned", "hash")
        self.store.remove_series(series_id)
        self.store.record_download(original, "late", "late-hash")
        self.assertEqual(self.store.load()["series"], [])
        self.assertEqual(self.store.load()["downloads"], [])

    def test_duplicate_name_and_season_are_rejected(self):
        self.store.add_series(self.fields)
        with self.assertRaises(ValueError):
            self.store.add_series(dict(self.fields, name="ExampleShow"))

    def test_concurrent_processes_do_not_lose_updates(self):
        self.store.update(lambda config: config.update(counter=0))
        script = """import sys, time
from store import ConfigStore
store = ConfigStore(sys.argv[1])
def change(config):
    value = config['counter']
    time.sleep(0.002)
    config['counter'] = value + 1
for _ in range(15): store.update(change)
"""
        workers = [subprocess.Popen([sys.executable, "-B", "-c", script, str(self.path)],
                                   cwd=Path(__file__).resolve().parents[1],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                   for _ in range(3)]
        try:
            for worker in workers:
                out, err = worker.communicate(timeout=20)
                self.assertEqual(worker.returncode, 0, err)
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                    worker.communicate()
        self.assertEqual(self.store.load()["counter"], 45)

    def test_process_lock_never_expires_while_owned(self):
        path = self.path.parent / "test.lock"
        lock = FileLock(path)
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)
        os.utime(path, (0, 0))
        script = """import sys
from pathlib import Path
from store import FileLock
lock = FileLock(Path(sys.argv[1]))
print(lock.acquire())
lock.release()
"""
        def attempt():
            return subprocess.run([sys.executable, "-B", "-c", script, str(path)],
                                  cwd=Path(__file__).resolve().parents[1],
                                  capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(attempt(), "False")
        lock.release()
        self.assertEqual(attempt(), "True")

    def test_failed_migration_write_keeps_original_and_backup(self):
        raw = json.dumps(self.legacy()).encode("utf-8")
        self.path.write_bytes(raw)
        with patch("store.os.replace", side_effect=PermissionError("busy")), patch("store.time.sleep"):
            with self.assertRaises(PermissionError):
                self.store.load()
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(self.path.with_name("series.json.v1.bak").read_bytes(), raw)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_concurrent_migration_produces_one_set_of_stable_ids(self):
        self.path.write_text(json.dumps(self.legacy()), encoding="utf-8")
        script = """import sys
from store import ConfigStore
print(ConfigStore(sys.argv[1]).load()['series'][0]['id'])
"""
        workers = [subprocess.Popen([sys.executable, "-B", "-c", script, str(self.path)],
                                   cwd=Path(__file__).resolve().parents[1],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                   for _ in range(3)]
        ids = set()
        try:
            for worker in workers:
                out, err = worker.communicate(timeout=20)
                self.assertEqual(worker.returncode, 0, err)
                ids.add(out.strip())
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                    worker.communicate()
        self.assertEqual(len(ids), 1)
        self.assertEqual(len(list(self.path.parent.glob("*.bak"))), 1)
