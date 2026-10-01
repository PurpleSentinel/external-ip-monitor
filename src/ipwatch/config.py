"""Strict YAML configuration; relative paths are relative to the YAML file."""
from dataclasses import dataclass
from email.utils import parseaddr
from pathlib import Path
import re
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


def address(value, name):
    value = string(value, name)
    _, addr = parseaddr(value)
    local, _, domain = addr.rpartition("@")
    if (not local or not domain or any(c in value for c in ",;")
            or any(c.isspace() for c in addr)):
        raise ConfigError(f"{name} must be a single email address, optionally with a display name")
    return value


@dataclass(frozen=True)
class EmailConfig:
    host: str
    port: int
    security: str
    ca_file: Path | None
    username: str | None
    password_file: Path | None
    password_env: str | None
    sender: str
    recipients: tuple[str, ...]
    subject_prefix: str
    timeout_seconds: int
    notify_on: tuple[str, ...] = ("ip_change",)


NOTIFY_TRIGGERS = ("ip_change", "country_change")


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
    email: EmailConfig | None = None


def load_email(section, resolve):
    """Return EmailConfig when enabled, else None; values are validated either way."""
    allowed = {"enabled", "notify_on", "smtp_host", "smtp_port", "security", "ca_file", "username",
        "password_file", "password_env", "from", "to", "subject_prefix", "timeout_seconds"}
    if isinstance(section, dict) and "password" in section:
        raise ConfigError("email.password is not supported; use email.password_file or email.password_env")
    em = mapping(section, "email", allowed)
    enabled = boolean(em.get("enabled", False), "email.enabled")
    notify_on = em.get("notify_on", ["ip_change"])
    if (not isinstance(notify_on, list) or not notify_on or len(set(notify_on)) != len(notify_on)
            or any(v not in NOTIFY_TRIGGERS for v in notify_on)):
        raise ConfigError("email.notify_on must be a nonempty list of ip_change and/or country_change")
    security = em.get("security", "starttls")
    if security not in ("starttls", "tls"):
        raise ConfigError("email.security must be starttls or tls; plaintext SMTP is not supported")
    host = em.get("smtp_host")
    if host is not None and (any(c.isspace() or c in "/@" for c in string(host, "email.smtp_host"))):
        raise ConfigError("email.smtp_host must be a hostname")
    port = integer(em.get("smtp_port", 587 if security == "starttls" else 465), "email.smtp_port", 1, 65535)
    ca_file = em.get("ca_file")
    ca_file = resolve(string(ca_file, "email.ca_file")) if ca_file is not None else None
    username = em.get("username")
    username = string(username, "email.username") if username is not None else None
    password_file = em.get("password_file")
    password_file = resolve(string(password_file, "email.password_file")) if password_file is not None else None
    password_env = em.get("password_env")
    if password_env is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", string(password_env, "email.password_env")):
        raise ConfigError("email.password_env must be an environment variable name")
    if username is not None and (password_file is None) == (password_env is None):
        raise ConfigError("email.username requires exactly one of email.password_file or email.password_env")
    if username is None and (password_file is not None or password_env is not None):
        raise ConfigError("email.password_file/password_env require email.username")
    sender = em.get("from")
    sender = address(sender, "email.from") if sender is not None else None
    recipients = em.get("to", [])
    if not isinstance(recipients, list) or len(recipients) > 10:
        raise ConfigError("email.to must be a list of up to 10 addresses")
    recipients = tuple(address(v, "email.to") for v in recipients)
    prefix = em.get("subject_prefix", "[ipwatch]")
    if prefix != "":
        prefix = string(prefix, "email.subject_prefix")
    timeout = integer(em.get("timeout_seconds", 20), "email.timeout_seconds", 1, 120)
    if not enabled:
        return None
    if host is None or sender is None or not recipients:
        raise ConfigError("Enabled email requires email.smtp_host, email.from and at least one email.to")
    return EmailConfig(host, port, security, ca_file, username, password_file, password_env,
        sender, recipients, prefix, timeout, tuple(notify_on))


def load_config(filename):
    source = Path(filename).expanduser().resolve()
    try:
        if source.stat().st_size > 65536:
            raise ConfigError("Configuration is larger than 64 KiB")
        data = yaml.load(source.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except (OSError, yaml.YAMLError, UnicodeError) as exc:
        raise ConfigError(f"Cannot read configuration: {exc}") from exc
    data = mapping(data, "config", {"interval_minutes", "output", "network", "geoip", "label", "email"})
    out = mapping(data.get("output", {}), "output", {"log_file", "state_file", "lock_file"})
    net = mapping(data.get("network", {}), "network", {"family", "endpoints", "curl_binary",
        "timeout_seconds", "connect_timeout_seconds", "attempts", "bypass_proxy", "interface"})
    geo = mapping(data.get("geoip", {}), "geoip", {"enabled", "endpoint", "cache_ttl_minutes"})
    def resolve(raw):
        return (source.parent / Path(raw).expanduser()).resolve()
    def path(key, default):
        return resolve(string(out.get(key, default), f"output.{key}"))
    log_file = path("log_file", "var/history.jsonl")
    state_file = path("state_file", "var/state.json")
    lock_file = path("lock_file", "var/run.lock")
    email = load_email(data.get("email", {}), resolve)
    paths = [log_file, state_file, lock_file, source]
    if email and email.password_file:
        paths.append(email.password_file)
    if len(set(paths)) != len(paths):
        raise ConfigError("Log, state, lock, configuration and password paths must be distinct")
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
    geo_enabled = boolean(geo.get("enabled", True), "geoip.enabled")
    if email and "country_change" in email.notify_on and not geo_enabled:
        raise ConfigError("email.notify_on: country_change requires geoip.enabled: true")
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
        geo_enabled, geo_endpoint,
        integer(geo.get("cache_ttl_minutes", 1440), "geoip.cache_ttl_minutes", 0, 525600),
        string(label, "label") if label is not None else None, email)
