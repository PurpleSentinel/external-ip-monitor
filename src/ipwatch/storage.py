"""Linux advisory locking, atomic state replacement, and durable JSONL append."""
from contextlib import contextmanager
import fcntl
import json
import math
import os
import tempfile


class StorageError(RuntimeError):
    pass


def prepare(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)


@contextmanager
def locked(path):
    prepare(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_state(path):
    try:
        with path.open(encoding="utf-8") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        return {"schema_version": 1, "last_attempt_epoch": None, "last_success_ip": None, "geo_cache": {},
            "last_country_code": None, "notify_pending": None}
    except (OSError, ValueError) as exc:
        raise StorageError("Cannot read state; repair or move the state file before retrying") from exc
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise StorageError("Unsupported or invalid state format")
    if not {"last_attempt_epoch", "last_success_ip", "geo_cache"} <= state.keys():
        raise StorageError("State is missing required fields")
    if type(state.get("last_attempt_epoch")) not in (int, float, type(None)):
        raise StorageError("Invalid state last_attempt_epoch")
    if state.get("last_attempt_epoch") is not None and not math.isfinite(state["last_attempt_epoch"]):
        raise StorageError("Invalid state last_attempt_epoch")
    if not isinstance(state.get("geo_cache"), dict):
        raise StorageError("Invalid state geo_cache")
    last_ip = state.get("last_success_ip")
    if last_ip is not None:
        import ipaddress
        try:
            ipaddress.ip_address(last_ip)
        except (ValueError, TypeError) as exc:
            raise StorageError("Invalid state last_success_ip") from exc
    # Optional fields added after 0.1.0: country baseline and change emails awaiting delivery.
    country = state.setdefault("last_country_code", None)
    if country is not None and (not isinstance(country, str) or not country):
        raise StorageError("Invalid state last_country_code")
    pending = state.setdefault("notify_pending", None)
    if pending is not None and not (isinstance(pending, dict) and isinstance(pending.get("notices"), list)
            and all(isinstance(n, dict) for n in pending["notices"]) and type(pending.get("attempts")) is int):
        raise StorageError("Invalid state notify_pending")
    return state


def write_state(path, state):
    prepare(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                prefix=".state-", delete=False) as handle:
            temporary = handle.name
            json.dump(state, handle, separators=(",", ":"), allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def append_record(path, record):
    prepare(path)
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n"
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
