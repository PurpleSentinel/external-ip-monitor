"""Strict YAML configuration; relative paths are relative to the YAML file."""
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
import yaml


class ConfigError(ValueError):
    pass


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate keys rather than silently selecting the last value."""


def unique_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ConfigError("YAML keys must be unique strings")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def mapping(value, name, allowed):
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping")
    unknown = set(value) - set(allowed)
    if unknown:
        raise ConfigError(f"Unknown keys in {name}: {sorted(map(str, unknown))}")
    return value


def integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f"{name} must be an integer from {low} to {high}")
    return value


def boolean(value, name):
    if type(value) is not bool:
        raise ConfigError(f"{name} must be true or false")
    return value


def string(value, name):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ConfigError(f"{name} must be a nonempty string without control characters")
    return value


def https_url(value, name):
    value = string(value, name)
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ConfigError(f"Invalid {name}") from exc
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.fragment or port == 0
            or any(c.isspace() for c in value)):
        raise ConfigError(f"{name} must be an HTTPS URL without credentials or fragment")
    return value


@dataclass(frozen=True)
class Config:
    source: Path
    interval_minutes: int
    log_file: Path
    state_file: Path
    lock_file: Path
    family: str
    endpoints: tuple[str, ...]
    curl_binary: str
    timeout_seconds: int
    connect_timeout_seconds: int
    attempts: int
    bypass_proxy: bool
    interface: str | None
    geo_enabled: bool
    geo_endpoint: str
    geo_cache_ttl_minutes: int
    label: str | None


def load_config(filename):
    source = Path(filename).expanduser().resolve()
    try:
        if source.stat().st_size > 65536:
            raise ConfigError("Configuration is larger than 64 KiB")
        data = yaml.load(source.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except (OSError, yaml.YAMLError, UnicodeError) as exc:
        raise ConfigError(f"Cannot read configuration: {exc}") from exc
    data = mapping(data, "config", {"interval_minutes", "output", "network", "geoip", "label"})
    out = mapping(data.get("output", {}), "output", {"log_file", "state_file", "lock_file"})
    net = mapping(data.get("network", {}), "network", {"family", "endpoints", "curl_binary",
        "timeout_seconds", "connect_timeout_seconds", "attempts", "bypass_proxy", "interface"})
    geo = mapping(data.get("geoip", {}), "geoip", {"enabled", "endpoint", "cache_ttl_minutes"})
    def path(key, default):
        raw = Path(string(out.get(key, default), f"output.{key}")).expanduser()
        return (source.parent / raw).resolve()
    log_file = path("log_file", "var/history.jsonl")
    state_file = path("state_file", "var/state.json")
    lock_file = path("lock_file", "var/run.lock")
    paths = [log_file, state_file, lock_file, source]
    if len(set(paths)) != len(paths):
        raise ConfigError("Log, state, lock and configuration paths must be distinct")
    family = net.get("family", "ipv4")
    if family not in ("ipv4", "ipv6"):
        raise ConfigError("network.family must be ipv4 or ipv6")
    default_endpoint = "https://api.ipify.org" if family == "ipv4" else "https://api6.ipify.org"
    endpoints = net.get("endpoints", [default_endpoint])
    if not isinstance(endpoints, list) or not 1 <= len(endpoints) <= 10:
        raise ConfigError("network.endpoints must contain 1 to 10 HTTPS URLs")
    endpoints = tuple(https_url(v, "network.endpoints") for v in endpoints)
    geo_endpoint = string(geo.get("endpoint", "https://ipwho.is/{ip}"), "geoip.endpoint")
    if geo_endpoint.count("{ip}") != 1 or "{" in geo_endpoint.replace("{ip}", "") or "}" in geo_endpoint.replace("{ip}", ""):
        raise ConfigError("geoip.endpoint must contain exactly one {ip} placeholder")
    https_url(geo_endpoint.replace("{ip}", "8.8.8.8"), "geoip.endpoint")
    timeout = integer(net.get("timeout_seconds", 15), "network.timeout_seconds", 1, 120)
    connect = integer(net.get("connect_timeout_seconds", 5), "network.connect_timeout_seconds", 1, timeout)
    interface = net.get("interface")
    label = data.get("label")
    return Config(source, integer(data.get("interval_minutes", 5), "interval_minutes", 1, 525600),
        log_file, state_file, lock_file, family, endpoints,
        string(net.get("curl_binary", "/usr/bin/curl"), "network.curl_binary"), timeout, connect,
        integer(net.get("attempts", 2), "network.attempts", 1, 3),
        boolean(net.get("bypass_proxy", True), "network.bypass_proxy"),
        string(interface, "network.interface") if interface is not None else None,
        boolean(geo.get("enabled", True), "geoip.enabled"), geo_endpoint,
        integer(geo.get("cache_ttl_minutes", 1440), "geoip.cache_ttl_minutes", 0, 525600),
        string(label, "label") if label is not None else None)
