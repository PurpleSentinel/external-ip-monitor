"""Command-line interface, suitable for a user crontab or systemd user timer."""
import argparse
from email.utils import parseaddr
import json
import shlex
import socket
import sys
from pathlib import Path
from . import __version__
from .config import ConfigError, load_config
from .monitor import run
from . import notify, systemd
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
                            ("cron-line", "Print a user-crontab entry; do not install it"),
                            ("systemd-units", "Print (or --write) a systemd user service and timer"),
                            ("test-email", "Send a test email using the email settings")]:
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--config", required=True, type=Path)
        if name == "run":
            command.add_argument("--force", action="store_true", help="Ignore interval, but retain the lock")
            command.add_argument("--stdout", action="store_true", help="Also print the new JSON record")
        if name == "systemd-units":
            command.add_argument("--name", default="ipwatch", help="Unit name, e.g. ipwatch-ipv6 (default: ipwatch)")
            command.add_argument("--write", action="store_true",
                help="Install into the systemd user unit directory; does not enable the timer")
            command.add_argument("--overwrite", action="store_true", help="With --write, replace changed unit files")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "check":
            if config.email:
                try:
                    # Local password and CA checks only; no network.
                    notify.read_password(config.email)
                    notify.tls_context(config.email)
                except notify.NotifyError as exc:
                    raise ConfigError(str(exc)) from exc
            print(f"Configuration valid: {config.source}")
            return 0
        if args.command == "test-email":
            if not config.email:
                raise ConfigError("Email is not enabled in this configuration")
            refused = notify.send(config.email, notify.compose_test(config.email, socket.gethostname(), config.label))
            delivered = [r for r in config.email.recipients if parseaddr(r)[1] not in refused]
            print(f"Test email accepted by {config.email.host} for: {', '.join(delivered)}")
            if refused:
                print(f"ipwatch: recipients refused: {', '.join(refused)}", file=sys.stderr)
                return 3
            return 0
        if args.command == "cron-line":
            print(cron_line(config))
            return 0
        if args.command == "systemd-units":
            files = systemd.units(config, args.name)
            if not args.write:
                for filename, content in files.items():
                    print(f"# {filename}\n{content}")
                return 0
            for target, status in systemd.write_units(files, systemd.user_unit_dir(), args.overwrite):
                print(f"{status}: {target}")
            print(f"Next: systemctl --user daemon-reload && systemctl --user enable --now {args.name}.timer")
            return 0
        record, code = run(config, force=args.force)
        if record is not None and args.stdout:
            print(json.dumps(record, separators=(",", ":"), allow_nan=False))
        return code
    except notify.NotifyError as exc:
        print(f"ipwatch: {exc}", file=sys.stderr)
        return 3
    except (ConfigError, StorageError, systemd.UnitError, OSError, ValueError) as exc:
        print(f"ipwatch: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
