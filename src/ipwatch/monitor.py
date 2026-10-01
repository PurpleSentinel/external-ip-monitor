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
# Undelivered change notices kept for the next email; IP-only notices are dropped first.
MAX_QUEUED_NOTICES = 50


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


def should_notify(email, record):
    return bool(("ip_change" in email.notify_on and record["changed"])
        or ("country_change" in email.notify_on and record["country_changed"]))


def queue_notice(state, record):
    # Queue every undelivered change in order, so a later IP-only change cannot hide
    # an earlier country change. The JSONL log remains the complete history.
    pending = state.get("notify_pending") or {"notices": [], "dropped": 0, "attempts": 0, "last_error": None}
    notices = pending["notices"]
    notices.append(notify.notice_from(record))
    while len(notices) > MAX_QUEUED_NOTICES:
        del notices[next((i for i, n in enumerate(notices) if not n["country_changed"]), 0)]
        pending["dropped"] = pending.get("dropped", 0) + 1
    state["notify_pending"] = pending


def deliver_pending(config, state, mailer):
    """Send all queued change notices in one email; return False if they stay queued."""
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
        previous_country = state["last_country_code"]
        record = {"schema_version": 1, "event": "external_ip_observation", "record_id": str(uuid.uuid4()),
            "timestamp_utc": timestamp(now), "hostname": socket.gethostname(), "label": config.label,
            "interval_minutes": config.interval_minutes, "family": config.family,
            "status": "error" if ip is None else "ok", "ip": ip,
            "previous_ip": previous, "changed": None if ip is None or previous is None else ip != previous,
            "country_code": None, "previous_country_code": previous_country, "country_changed": None,
            "ip_source": endpoint, "geoip": {"status": "not_attempted", "provider": None,
                "cache_hit": False, "lookup_at_utc": None, "data": None}, "errors": errors}
        if ip is not None:
            record["geoip"], geo_error = enrich(client, config, state, ip, now)
            if geo_error:
                record["errors"].append(geo_error)
                record["status"] = "partial"
            country = (record["geoip"]["data"] or {}).get("country_code")
            if country:
                record["country_code"] = country
                record["country_changed"] = None if previous_country is None else country != previous_country
        append_record(config.log_file, record)
        if ip is not None:
            state["last_success_ip"] = ip
        if record["country_code"]:
            # Unknown country (GeoIP disabled or failed) leaves the baseline intact, so a
            # change is reported late, at the next successful lookup, rather than missed.
            state["last_country_code"] = record["country_code"]
        if config.email and should_notify(config.email, record):
            # Queue before sending: an undelivered notice survives failures and crashes.
            queue_notice(state, record)
        write_state(config.state_file, state)
        if ip is None:
            return record, 1
        if config.email and not deliver_pending(config, state, mailer):
            return record, 3
        return record, 0
