"""systemd user service and timer units: an alternative to cron for hosts without it."""
import os
from pathlib import Path
import re
import sys
from .config import ConfigError

NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class UnitError(RuntimeError):
    pass


def escape(text):
    # systemd expands %-specifiers in most settings and $VARIABLES in ExecStart.
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise ConfigError("Paths used in systemd units cannot contain control characters")
    return text.replace("%", "%%")


def exec_arg(arg):
    arg = escape(arg).replace("$", "$$")
    if arg and not any(c.isspace() or c in "\"'\\;" for c in arg):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def units(config, name="ipwatch"):
    """Return {filename: content} for the service and timer."""
    if not NAME_PATTERN.fullmatch(name):
        raise ConfigError("Unit name must be 1-64 letters, digits, '_', '.' or '-', starting with a letter or digit")
    # Invoke the exact interpreter currently running, including a virtualenv.
    command = " ".join(exec_arg(a) for a in [sys.executable, "-m", "ipwatch", "run", "--config", str(config.source)])
    service = f"""[Unit]
Description=ipwatch external IP observation ({escape(str(config.source))})
Documentation=https://github.com/PurpleSentinel/external-ip-monitor

[Service]
Type=oneshot
ExecStart={command}
# 1: IP discovery failed (an error record was written). 3: change email queued for retry.
SuccessExitStatus=1 3
# The tool bounds its own requests; this only stops a run that is stuck regardless.
TimeoutStartSec=30min
NoNewPrivileges=yes
"""
    timer = f"""[Unit]
Description=Run {name}.service every minute; interval_minutes in the YAML decides when a sample is due

[Timer]
OnCalendar=*-*-* *:*:00
# Fire on the minute, like cron. The default AccuracySec=1min would spread runs across
# the minute, which the tool's 30-second due-time grace does not absorb.
AccuracySec=1s

[Install]
WantedBy=timers.target
"""
    return {f"{name}.service": service, f"{name}.timer": timer}


def user_unit_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def write_units(files, directory, overwrite=False):
    """Write unit files; refuse to replace different content unless overwrite is set."""
    for filename, content in files.items():
        target = directory / filename
        if target.exists() and not overwrite and target.read_text(encoding="utf-8") != content:
            raise UnitError(f"{target} exists with different content; rerun with --overwrite to replace it")
    directory.mkdir(parents=True, exist_ok=True)
    results = []
    for filename, content in files.items():
        target = directory / filename
        if target.exists() and target.read_text(encoding="utf-8") == content:
            results.append((target, "unchanged"))
            continue
        temporary = target.with_name(f".{filename}.tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, target)
        results.append((target, "written"))
    return results
