# External IP Monitor

A small Linux CLI that records the public IP seen by an Internet service, enriches it with GeoIP data, and appends one serialized JSON object per line. Designed to run every minute from cron or a systemd user timer, including on Starlink connections, with no GUI or daemon.

## Features

- YAML configuration, including the sampling interval in minutes.
- Actual `curl` execution for IP discovery and GeoIP HTTPS requests.
- IPv4 or IPv6, selectable per configuration; optional interface binding.
- Ordered discovery endpoints, bounded timeouts and configurable attempts.
- Country, region, city, coordinates, timezone, ASN, ISP and organization enrichment.
- GeoIP caching across invocations, with configurable refresh time.
- Scheduling by cron (`cron-line`) or a systemd user timer (`systemd-units`), for distributions without cron such as Fedora Workstation.
- Every scheduled observation retained, even when the IP is unchanged.
- IP change detection against the last successful observation.
- GeoIP country change detection, recorded on every observation.
- Optional email on IP change, GeoIP country change, or both, over verified TLS (STARTTLS or implicit TLS), with queued retry.
- Failure records, and partial records when GeoIP enrichment is unavailable.
- Linux process locking and atomic state replacement.
- Unit and local HTTPS integration tests.

## Quick start

Requires Linux, Python 3.10+, `curl`, and Python virtual environment support. On Debian/Ubuntu:

```bash
sudo apt-get update
sudo apt-get install python3 python3-venv curl
```

Clone the repository, enter it, then:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
cp config.example.yaml config.yaml
# Edit config.yaml as needed.
.venv/bin/ipwatch check --config "$PWD/config.yaml"
.venv/bin/ipwatch run --config "$PWD/config.yaml" --force --stdout
```

Then schedule it to run every minute, with **either** a systemd user timer (no root needed; the choice on systems without cron, such as Fedora Workstation):

```bash
.venv/bin/ipwatch systemd-units --config "$PWD/config.yaml" --write
systemctl --user daemon-reload
systemctl --user enable --now ipwatch.timer
```

**or** cron, by pasting the output of `.venv/bin/ipwatch cron-line --config "$PWD/config.yaml"` into `crontab -e` for the same user.

Both commands generate entries with absolute paths to the current Python interpreter and YAML file. `cron-line` only prints; `systemd-units --write` installs the unit files but does not enable them. See [SETUP.md](SETUP.md#4-schedule-it) for details, and [docs/SYSTEMD.md](docs/SYSTEMD.md) for the systemd timer in depth.

The scheduler calls the tool every minute. `interval_minutes` in YAML determines when a sample is due. Set it to `5`, `7`, `90`, or any positive whole number up to 525600. A sample is due once the interval minus 30 seconds has passed since the previous attempt started; the slack absorbs Python start-up jitter so a 5-minute interval does not slip to 6 minutes. There is no need to change the schedule when changing the interval. Avoid combining this gate with an `*/X` cron entry or a less frequent timer, which can delay observations further.

Default output is `var/history.jsonl`, relative to the YAML file's directory. Each newly written record occupies exactly one line. State and lock files live alongside it by default. The CLI stays silent during normal runs unless `--stdout` is supplied.

See [SETUP.md](SETUP.md) for Linux setup, scheduling (systemd timer or cron), rotation, IPv6, email notifications and troubleshooting.

## Example record

The following is an illustrative record, formatted here for readability. The actual log writes compact, single-line JSON. See [examples/observation.jsonl](examples/observation.jsonl).

```json
{
  "schema_version": 1,
  "event": "external_ip_observation",
  "timestamp_utc": "2026-10-01T13:00:00Z",
  "status": "ok",
  "family": "ipv4",
  "ip": "8.8.8.8",
  "previous_ip": "1.1.1.1",
  "changed": true,
  "country_code": "XX",
  "previous_country_code": "YY",
  "country_changed": true,
  "geoip": {
    "status": "ok",
    "provider": "ipwhois",
    "cache_hit": false,
    "lookup_at_utc": "2026-10-01T13:00:00Z",
    "data": {
      "country": "Example country",
      "country_code": "XX",
      "city": "Example city",
      "asn": 15169,
      "isp": "Example ISP"
    }
  },
  "errors": []
}
```

The full schema, including nullable fields, is described in [docs/LOG_FORMAT.md](docs/LOG_FORMAT.md).

## What the observation means

This records the network egress IP of the Linux host making the request. It does not query the Starlink router or reveal a router's private WAN address. Under CGNAT, the public IPv4 can be shared. A VPN, policy route or transparent gateway can change the observed egress. HTTP proxy environment variables are bypassed by default; set `network.bypass_proxy: false` to deliberately use them.

GeoIP describes a provider's estimate for the public IP, which may reflect an ISP exit location rather than your premises. It is not GPS or proof of subscriber identity. Discovery and enrichment send requests to the configured third-party services. Logs remain local.

The default discovery service is [ipify](https://www.ipify.org/). GeoIP uses the free [ipwho.is endpoint](https://ipwhois.io/documentation); consult its current terms, availability and quotas before deployment. Caching reduces requests, but rapidly changing IPs or many machines sharing an egress can still exhaust a quota. An HTTPS endpoint compatible with the ipwho.is schema can be substituted in YAML. Arbitrary provider schemas need a new adapter.

## Email notifications

Optionally, the tool emails you when the observed IP changes, when its GeoIP country changes, or both, selected with `email.notify_on`. It is off by default. When enabled:

- Mail is sent only over TLS: `starttls` (usually port 587) or `tls` (implicit TLS, usually port 465). There is no plaintext mode. The server certificate and hostname are verified against the system CA store, or a `ca_file` you supply, with TLS 1.2 or newer. If a server does not offer STARTTLS, nothing is sent, and the password is never transmitted.
- The SMTP password is read from a `password_file` (a regular file owned by you with mode 0600) or an environment variable, never from the YAML file.
- The email is sent only after the change is written to the JSONL log. If sending fails, the notice stays queued in the state file and is retried at the next due sample; the run exits with code 3.

`ipwatch test-email` checks the whole path: connection, TLS, login and delivery. See [SETUP.md](SETUP.md#7-email-notifications-optional) for setup and provider examples.

## Commands and exit codes

| Command | Purpose |
| --- | --- |
| `ipwatch run --config /absolute/config.yaml` | Sample if due; append JSONL |
| `ipwatch run --config /absolute/config.yaml --force --stdout` | Sample immediately and also print the record |
| `ipwatch check --config /absolute/config.yaml` | Validate configuration without network calls or writes |
| `ipwatch cron-line --config /absolute/config.yaml` | Print a user-crontab entry |
| `ipwatch systemd-units --config /absolute/config.yaml [--write] [--name N]` | Print, or install, a systemd user service and timer |
| `ipwatch test-email --config /absolute/config.yaml` | Send a test email with the configured SMTP settings |
| `ipwatch --version` | Show version |

| Exit code | Meaning |
| --- | --- |
| 0 | IP recorded, optional GeoIP failure, not due, or another run holds the lock |
| 1 | IP discovery failed; an error record was appended |
| 2 | Configuration or storage error; inspect stderr |
| 3 | IP recorded, but the change email failed (queued for retry) or some recipients were refused; inspect stderr |

## Repository layout

```text
external-ip-monitor/
  README.md
  SETUP.md
  CHANGELOG.md
  LICENSE
  .gitignore
  pyproject.toml
  config.example.yaml
  src/ipwatch/
  tests/
  docs/
  examples/
```

## Development and future extensions

```bash
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

The test suite uses fake responses and a local HTTPS server, including real curl subprocesses. It does not require public API connectivity or consume provider quotas. The HTTPS tests require `openssl`; they skip when curl or openssl is missing.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for module boundaries and extension points. No database, alerting service, web server or background process is required for this release.

## Keeping private data out of Git

Your live `config.yaml` and everything under `var/` (history, state with the GeoIP cache, lock) record your public IP and estimated location. The repository's `.gitignore` excludes them, along with the virtualenv and build artifacts, so `git add .` does not pick them up. If you point `output.*` paths elsewhere inside the repository, add those paths to `.gitignore` too, and check `git status` before committing.

## License

Released under the [MIT License](LICENSE).
