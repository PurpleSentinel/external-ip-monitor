# Changelog

## Unreleased

- Add optional email notification when the observed IP changes (`email` YAML section, off by default). Uses SMTP over STARTTLS or implicit TLS only, with certificate and hostname verification and TLS 1.2 minimum, and stops before login if STARTTLS is not offered.
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
