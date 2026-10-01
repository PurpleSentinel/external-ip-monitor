# Architecture and extension points

The process runs once and exits. Cron handles repeated invocation; an interval gate reads state under a nonblocking Linux `flock`. The lock remains held through network requests, append and state update. Independent configs need independent output paths.

1. CLI parses a command and validates YAML.
2. `run` acquires the lock, reads state and checks whether a sample is due (`interval_minutes × 60 − DUE_GRACE_SECONDS` since the last attempt; the 30-second grace absorbs cron start-up jitter).
3. It atomically reserves the attempt start time.
4. `discover` requests configured endpoints in order with curl, enforcing family and public-address validation.
5. The enrichment adapter looks up the accepted IP or uses a valid cached lookup.
6. The record is serialized and appended to the log, flushed and fsynced.
7. Country change is computed against the last known country code. State is atomically replaced with the updated IP and country baselines and cache. If email is enabled and a configured trigger (`ip_change`, `country_change`) fired, the notice is appended to the queue in this same state write.
8. All queued notices are sent in one email over verified TLS. On success the queue is cleared; on failure it stays queued for the next due sample. The lock is released.

| Module | Responsibility | Extension approach |
| --- | --- | --- |
| `config.py` | YAML loading, strict types, defaults and path resolution | Add documented, validated options |
| `transport.py` | Bounded HTTPS curl subprocesses | Add explicit network features without arbitrary shell flags |
| `geo.py` | ipwho.is-compatible provider response normalization | Add adapter dispatch for other HTTP providers or local MMDB databases |
| `storage.py` | Locking, JSONL append, atomic JSON state | Add remote forwarding or database sink as a separate layer |
| `monitor.py` | Scheduling gate, observation, IP and country change detection, caching, notification queue | Add further alert channels after a successful durable observation |
| `notify.py` | Email composition, password loading, TLS-verified SMTP delivery | Add other notification transports behind the same queue |
| `cli.py` | Commands, exit codes and generated cron line | Add export/query commands without altering cron behavior |

No provider SDK is required; PyYAML is the only Python runtime dependency. Email uses the standard library's `smtplib`, `ssl` and `email` modules. Linux-specific `fcntl`, `/proc/self/fd`, and directory fsync are deliberate because this release targets Linux.

The HTTP response body is limited to 64 KiB and requests have connection, transfer and process deadlines. Curl runs without a shell, ignores `.curlrc`, allows only HTTPS and verifies TLS. It does not follow redirects. There is no custom daemon, in-memory-only scheduling state, or graphical interface.

The log uses a versioned public schema, while state also has its own schema version. Consumers should accept added optional fields within a schema version and reject unsupported major changes. Keep normalized provider fields stable when adding providers. Migrate state explicitly if changing its format; the current code refuses unsupported versions.

Potential later releases include local GeoIP databases, dual-family samples in one event, further notification channels, remote log shipping, signed records, CSV exports and a small query command. These are extension points rather than implemented features.

The current tool cannot distinguish whether an observed IPv4 changed because of a CGNAT exit, VPN, ISP address assignment or routing change. It records what the chosen external endpoint sees.

## Verification

The suite covers arbitrary-minute intervals, cross-hour behavior, start-up jitter around the due boundary, force runs, backwards clocks, change detection across failures, IPv6, invalid/nonpublic addresses, fallback endpoints, GeoIP response validation, cache TTL and size bounds, malformed YAML/state, log write failures, permission defaults, process contention, and cron path quoting. Email tests cover configuration validation, `notify_on` trigger selection, country baselines across GeoIP failures, queue ordering and overflow, password-file permissions, message content, queueing and retry, and old-state compatibility. They also drive real `smtplib` against a local SMTP server over STARTTLS and implicit TLS, checking certificate rejection, a server without STARTTLS, failed authentication and partially refused recipients.

Process-boundary tests use a fake curl executable to inspect arguments and a local TLS server to exercise the real curl executable through the installed CLI. Local TLS checks verify both accepted certificates and rejection with an invalid CA bundle. Public provider availability is an operational dependency, not a condition for these deterministic tests.
