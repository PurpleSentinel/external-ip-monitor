"""Command-line interface, suitable for a user crontab."""
import argparse
import json
import shlex
import sys
from pathlib import Path
from . import __version__
from .config import ConfigError, load_config
from .monitor import run
from .storage import StorageError


def cron_line(config):
    # Invoke the exact interpreter currently running, including a virtualenv.
    command = shlex.join([sys.executable, "-m", "ipwatch", "run", "--config", str(config.source)])
    # cron treats percent specially even inside shell quotes.
    return "* * * * * " + command.replace("%", "\\%")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Record public IP and GeoIP history as JSON Lines")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in [("run", "Record one observation if due"),
                            ("check", "Validate YAML without network requests or writes"),
                            ("cron-line", "Print a user-crontab entry; do not install it")]:
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--config", required=True, type=Path)
        if name == "run":
            command.add_argument("--force", action="store_true", help="Ignore interval, but retain the lock")
            command.add_argument("--stdout", action="store_true", help="Also print the new JSON record")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "check":
            print(f"Configuration valid: {config.source}")
            return 0
        if args.command == "cron-line":
            print(cron_line(config))
            return 0
        record, code = run(config, force=args.force)
        if record is not None and args.stdout:
            print(json.dumps(record, separators=(",", ":"), allow_nan=False))
        return code
    except (ConfigError, StorageError, OSError, ValueError) as exc:
        print(f"ipwatch: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
