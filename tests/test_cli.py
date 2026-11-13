import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import watcher
from engine import WatcherEngine
from store import ConfigStore


class CliTests(unittest.TestCase):
    def test_entry_point_runs_shared_engine_with_empty_isolated_store(self):
        with tempfile.TemporaryDirectory(prefix="watcher-cli-tests-") as directory:
            store = ConfigStore(Path(directory) / "series.json")
            engine = WatcherEngine(store)
            output = io.StringIO()
            with patch.object(watcher, "WatcherEngine", return_value=engine), contextlib.redirect_stdout(output):
                self.assertEqual(watcher.main(["--dry-run"]), 0)
            self.assertIn("No series to watch", output.getvalue())
            self.assertFalse(store.path.exists())

    def test_subprocess_uses_explicit_store_path(self):
        with tempfile.TemporaryDirectory(prefix="watcher-cli-tests-") as directory:
            path = Path(directory) / "series.json"
            command = [sys.executable, "-B", str(Path(watcher.__file__)),
                       "--config", str(path), "--dry-run"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("No series to watch", result.stdout)
            self.assertFalse(path.exists())

    def test_schedule_preserves_absolute_config_path_with_spaces(self):
        relative_path = Path("watch lists") / "custom series.json"
        result = SimpleNamespace(returncode=0, stdout="Created", stderr="")
        with patch.object(watcher.subprocess, "run", return_value=result) as run, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(watcher.main(["--config", str(relative_path), "--install-schedule"]), 0)
        arguments = run.call_args.args[0]
        command = arguments[arguments.index("/TR") + 1]
        self.assertIn(f'--config "{relative_path.resolve()}"', command)
        self.assertIn(str(Path(watcher.__file__).resolve()), command)

    def test_default_schedule_keeps_default_config(self):
        result = SimpleNamespace(returncode=0, stdout="Created", stderr="")
        with patch.object(watcher.subprocess, "run", return_value=result) as run, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(watcher.main(["--install-schedule"]), 0)
        arguments = run.call_args.args[0]
        command = arguments[arguments.index("/TR") + 1]
        self.assertNotIn("--config", command)
