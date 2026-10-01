"""One observation per due invocation; no daemon or in-process scheduler."""
from datetime import datetime, timezone
import ipaddress
import math
import socket
import sys
import time
import uuid
from . import notify
from .geo import GeoError, lookup
from .storage import append_record, locked, read_state, write_state
from .transport import CurlClient, RequestError

# Cron fires on the minute, but each run records its attempt time a variable
# fraction of a second later. Without slack, a run that starts marginally earlier
# than the previous one sees 299.9 s for a 5-minute interval and slips a minute.
DUE_GRACE_SECONDS = 30


def timestamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def discover(client, config):
    errors = []
    for endpoint in config.endpoints:
        try:
            value = client.get(endpoint, config.family).strip()
            if len(value) > 64:
                raise ValueError("IP response is too long")
            address = ipaddress.ip_address(value)
            if address.version != (4 if config.family == "ipv4" else 6):
                raise ValueError("Endpoint returned the wrong address family")
            if not address.is_global or address.is_multicast:
                raise ValueError("Endpoint did not return a public unicast IP")
            return str(address), endpoint, errors
        except (RequestError, ValueError) as exc:
            errors.append({"stage": "ip_discovery", "message": str(exc) if isinstance(exc, RequestError)
                else "Endpoint returned an invalid, non-public, or wrong-family IP"})
    return None, None, errors


def enrich(client, config, state, ip, now):
    if not config.geo_enabled:
        return {"status": "disabled", "provider": None, "cache_hit": False,
            "lookup_at_utc": None, "data": None}, None
    result = {"status": "error", "provider": "ipwhois", "cache_hit": False,
        "lookup_at_utc": None, "data": None}
    cache = state["geo_cache"]
    cached = cache.get(ip)
    if (isinstance(cached, dict) and type(cached.get("epoch")) in (int, float)
            and math.isfinite(cached["epoch"])
            and isinstance(cached.get("data"), dict) and cached.get("endpoint") == config.geo_endpoint
            and 0 <= now - cached["epoch"] < config.geo_cache_ttl_minutes * 60):
        result.update(status="ok", cache_hit=True, lookup_at_utc=timestamp(cached["epoch"]), data=cached["data"])
        return result, None
    try:
        data = lookup(client, config.geo_endpoint, ip)
    except GeoError as exc:
        return result, {"stage": "geoip", "message": str(exc)}
    result.update(status="ok", data=data, lookup_at_utc=timestamp(now))
    # Keep at most 64 successful lookups; no failed or stale results masquerade as fresh.
    cache.pop(ip, None)
    cache[ip] = {"epoch": now, "endpoint": config.geo_endpoint, "data": data}
    while len(cache) > 64:
        del cache[next(iter(cache))]
    return result, None


def warn(message):
    print(f"ipwatch: {message}", file=sys.stderr)


def deliver_pending(config, state, mailer):
    """Send any queued IP-change email; return False if it stays queued for retry."""
    pending = state.get("notify_pending")
    if not pending:
        return True
    try:
        refused = mailer(config.email, notify.compose(config.email, pending))
    except notify.NotifyError as exc:
        pending["attempts"] += 1
        pending["last_error"] = str(exc)
        write_state(config.state_file, state)
        warn(f"email notification failed; will retry at the next due sample: {exc}")
        return False
    if refused:
        warn(f"email notification refused for: {', '.join(refused)}")
    state["notify_pending"] = None
    write_state(config.state_file, state)
    return True


def run(config, force=False, client=None, now=None, mailer=None):
    """Return (record or None, exit code). Non-due/locked runs are successful no-ops."""
    client = client or CurlClient(config)
    mailer = mailer or notify.send
    with locked(config.lock_file) as acquired:
        if not acquired:
            return None, 0
        now = time.time() if now is None else now
        state = read_state(config.state_file)
        previous_attempt = state["last_attempt_epoch"]
        if (not force and previous_attempt is not None
                and 0 <= now - previous_attempt < config.interval_minutes * 60 - DUE_GRACE_SECONDS):
            return None, 0
        # Reserve this interval even when connectivity is down. A killed process can
        # leave a gap, but cannot trigger requests on every subsequent cron tick.
        state["last_attempt_epoch"] = now
        write_state(config.state_file, state)
        ip, endpoint, errors = discover(client, config)
        previous = state["last_success_ip"]
        record = {"schema_version": 1, "event": "external_ip_observation", "record_id": str(uuid.uuid4()),
            "timestamp_utc": timestamp(now), "hostname": socket.gethostname(), "label": config.label,
            "interval_minutes": config.interval_minutes, "family": config.family,
            "status": "error" if ip is None else "ok", "ip": ip,
            "previous_ip": previous, "changed": None if ip is None or previous is None else ip != previous,
            "ip_source": endpoint, "geoip": {"status": "not_attempted", "provider": None,
                "cache_hit": False, "lookup_at_utc": None, "data": None}, "errors": errors}
        if ip is not None:
            record["geoip"], geo_error = enrich(client, config, state, ip, now)
            if geo_error:
                record["errors"].append(geo_error)
                record["status"] = "partial"
        append_record(config.log_file, record)
        if ip is not None:
            state["last_success_ip"] = ip
        if config.email and record["changed"]:
            # Queue before sending: an undelivered notice survives failures and crashes.
            # A newer change replaces an older undelivered one; the log keeps both.
            state["notify_pending"] = {"notice": notify.notice_from(record), "attempts": 0, "last_error": None}
        write_state(config.state_file, state)
        if ip is None:
            return record, 1
        if config.email and not deliver_pending(config, state, mailer):
            return record, 3
        return record, 0
