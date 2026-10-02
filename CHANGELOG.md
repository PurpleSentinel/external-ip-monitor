# Changelog

## 1.0.1 - 2026-10-02

- Add the MIT `LICENSE` and declare it in package metadata (`license = "MIT"`).

## 1.0.0 - 2026-10-01

- Add [docs/SYSTEMD.md](docs/SYSTEMD.md): how the systemd timer works; behaviour across login, logout, lingering, boot and suspend; control commands; drop-in customisation; logs, exit codes and troubleshooting.
- Add `ipwatch systemd-units` to print, or `--write`, a systemd user service and timer, for hosts without cron such as Fedora Workstation. The timer fires on the minute (`AccuracySec=1s`), and exit codes 1 and 3 count as success. Paths are escaped for systemd's `%` and `$` handling. Installation never silently replaces changed unit files.
- Record GeoIP country change on every observation: new `country_code`, `previous_country_code` and `country_changed` log fields, with the baseline kept in state (`last_country_code`). A GeoIP failure does not reset the baseline.
- Add optional email notification (`email` YAML section, off by default), triggered by IP change, GeoIP country change, or both (`email.notify_on`). Undelivered notices queue in order (up to 50), so a later IP-only change cannot hide an earlier country change. Uses SMTP over STARTTLS or implicit TLS only, with certificate and hostname verification and TLS 1.2 minimum, and stops before login if STARTTLS is not offered.
- The SMTP password comes from a 0600 file owned by the user, or an environment variable; inline YAML passwords are rejected.
- Email is sent after the observation is durably logged. Failed deliveries stay queued in state (`notify_pending`) and retry at the next due sample. New exit code 3.
- New `ipwatch test-email` command. `check` now validates the password source and `ca_file` when email is enabled.
- Fix: a sample is now due 30 seconds before the full interval elapses. Previously, variable start-up time under a per-minute cron entry made roughly half of 5-minute samples arrive after 6 minutes.
- Add `.gitignore` so live configuration, collected IP/GeoIP history, the virtualenv and build artifacts are not committed.
- Docs: describe the due-time grace, replace ZIP/upload instructions with clone instructions, and remove references to a CI workflow and LICENSE file that are not in the repository.

## 0.1.0 - 2026-10-01

- Initial Linux cron-driven external IP monitor.
- YAML scheduling and network configuration.
- Curl-based IPv4/IPv6 discovery and GeoIP enrichment.
- JSON Lines history, persistent change detection and GeoIP cache.
- Locking, configuration validation and bounded network requests.
- Setup guide, architecture and record-format documentation, and tests.
