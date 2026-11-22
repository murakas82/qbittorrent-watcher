"""qBittorrent Watcher CLI.

python watcher.py                    search and add new episodes
python watcher.py --dry-run          search without adding or removing torrents
python watcher.py --install-schedule install the hourly Windows task
python watcher.py --remove-schedule  remove the hourly Windows task

Exit codes: 0 done, 1 qBittorrent not reachable, 2 watch list unreadable,
3 no search plugin enabled in qBittorrent.
"""

import argparse
import subprocess
import sys
from pathlib import Path

from engine import WatcherEngine
from store import ConfigStore

TASK_NAME = "qBittorrent Watcher"


def install_schedule(task_name=TASK_NAME, config_path=None):
    """Create a Windows scheduled task that runs the watcher every hour."""
    # pythonw.exe runs without opening a console window.
    python = Path(sys.executable).with_name("pythonw.exe")
    if not python.exists():
        python = Path(sys.executable)
    arguments = [str(python), str(Path(__file__).resolve())]
    if config_path is not None:
        arguments.extend(["--config", str(Path(config_path).resolve())])
    command = subprocess.list2cmdline(arguments)
    done = subprocess.run(
        ["schtasks", "/Create", "/TN", task_name, "/TR", command, "/SC", "HOURLY", "/F"],
        capture_output=True,
        text=True,
    )
    print((done.stdout or done.stderr).strip())
    return done.returncode


def remove_schedule(task_name=TASK_NAME):
    done = subprocess.run(
        ["schtasks", "/Delete", "/TN", task_name, "/F"], capture_output=True, text=True
    )
    print((done.stdout or done.stderr).strip())
    return done.returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Path to the watch list (defaults to series.json beside the program)")
    options = parser.add_mutually_exclusive_group()
    options.add_argument("--dry-run", action="store_true")
    options.add_argument("--install-schedule", action="store_true")
    options.add_argument("--remove-schedule", action="store_true")
    args = parser.parse_args(argv)
    if args.install_schedule:
        return install_schedule(config_path=args.config)
    if args.remove_schedule:
        return remove_schedule()
    try:
        return WatcherEngine(ConfigStore(args.config)).check(dry_run=args.dry_run)
    except (OSError, ValueError) as error:
        print(f"Could not run the watcher: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
