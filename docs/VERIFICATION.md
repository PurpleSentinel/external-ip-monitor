# Verification for version 0.1.0

Verified on Linux on 2026-10-01 with Python 3.12.14, PyYAML 6.0.3 and curl 8.5.0.

- All 26 unittest tests passed, with no skipped tests.
- Local HTTPS integration exercised the real curl binary through the CLI, including enrichment, JSONL append, interval skip, forced observation and cache reuse.
- HTTP error handling and TLS verification failure were exercised against the local server.
- A separate process verified that a held lock prevents concurrent writes.
- Packaging built a wheel successfully and installed it into a separate virtual environment.
- The installed command validated the example YAML, reported version 0.1.0 and generated an absolute-path cron entry.

The tests do not make public API calls. Default endpoint selection and response mapping were checked against the providers' documentation:

- https://www.ipify.org/
- https://ipwhois.io/documentation
- https://curl.se/docs/manpage.html

Public provider availability, quotas and geolocation accuracy remain runtime dependencies. Cron itself was not installed or changed in the development environment. No CI workflow is included in the repository; only the local runs listed here were executed.

## Unreleased changes

Verified on Linux (Fedora 44) on 2026-10-01 with Python 3.14.7, OpenSSL 3.5.8 and PyYAML 6.0.3: all 57 unittest tests passed, with ResourceWarnings treated as errors. This includes a regression test for start-up jitter at the interval boundary and the email notification and country-change tests. SMTP integration tests run real `smtplib` against a local TLS SMTP server; no external mail provider was contacted.

To reproduce:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

Install curl and openssl to include local HTTPS integration checks.
