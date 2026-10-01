# YAML configuration reference

Copy the supplied `config.example.yaml`. Omitted sections use defaults. Unknown keys, duplicate keys, invalid types, empty values and unsafe YAML tags are rejected. Configuration must be at most 64 KiB. Booleans must be YAML booleans, not quoted strings. Null sections are not mappings; omit a section or use `{}`.

| Key | Default | Meaning |
| --- | --- | --- |
| `interval_minutes` | `5` | Whole minutes between attempt starts; 1 to 525600. A run is due 30 seconds early to absorb cron start-up jitter |
| `label` | `null` | Optional nonempty name for this connection |
| `output.log_file` | `var/history.jsonl` | Append-only JSON Lines history |
| `output.state_file` | `var/state.json` | Last attempt, last successful IP and GeoIP cache |
| `output.lock_file` | `var/run.lock` | Advisory process lock |
| `network.family` | `ipv4` | `ipv4` or `ipv6` |
| `network.endpoints` | ipify URL for selected family | Ordered list of 1 to 10 HTTPS plain-text IP endpoints |
| `network.curl_binary` | `/usr/bin/curl` | Executable path or name resolved via PATH |
| `network.timeout_seconds` | `15` | Per-request timeout, 1 to 120 seconds |
| `network.connect_timeout_seconds` | `5` | Connection timeout, 1 to request timeout |
| `network.attempts` | `2` | Total HTTP attempts per endpoint, 1 to 3 |
| `network.bypass_proxy` | `true` | Add `--noproxy '*'`; false allows curl's proxy environment |
| `network.interface` | `null` | Optional interface passed as one curl argument |
| `geoip.enabled` | `true` | Enrich successful observations |
| `geoip.endpoint` | `https://ipwho.is/{ip}` | Exactly one `{ip}` placeholder; ipwho.is-compatible JSON |
| `geoip.cache_ttl_minutes` | `1440` | Successful lookup reuse; 0 to 525600; zero disables reuse |
| `email.enabled` | `false` | Send change emails |
| `email.notify_on` | `[ip_change]` | Nonempty list: `ip_change` (observed IP differs from the last successful one) and/or `country_change` (GeoIP country code differs from the last known one). `country_change` requires `geoip.enabled: true` |
| `email.smtp_host` | none | SMTP server hostname; required when enabled |
| `email.smtp_port` | `587` (`starttls`) or `465` (`tls`) | 1 to 65535 |
| `email.security` | `starttls` | `starttls` (upgrade a plain connection; refused if not offered) or `tls` (implicit TLS). No plaintext option |
| `email.ca_file` | `null` | PEM CA bundle that replaces the system CA store for SMTP; relative to the YAML file |
| `email.username` | `null` | SMTP login name; `null` sends without authentication |
| `email.password_file` | `null` | File whose first line is the password; must be a regular file owned by the user, mode 0600, not a symlink |
| `email.password_env` | `null` | Environment variable holding the password; exactly one of this or `password_file` when `username` is set |
| `email.from` | none | Sender address, optionally `Name <address>`; required when enabled |
| `email.to` | `[]` | 1 to 10 recipient addresses; required when enabled |
| `email.subject_prefix` | `[ipwatch]` | Prepended to the subject; `""` for none |
| `email.timeout_seconds` | `20` | Per SMTP network operation, 1 to 120 |

Output paths and the configuration's own path must be distinct. Relative output paths resolve against the YAML file's parent. `~` expands to the executing user's home. An absolute curl path is recommended for cron. Use UTF-8 YAML.

HTTPS URLs must contain a hostname, cannot contain credentials or fragments, and cannot contain whitespace. Redirects are not followed; configure the final endpoint directly. Discovery expects only a public unicast IP, with optional surrounding whitespace. JSON discovery responses are not supported in version 0.1.0. ipify's plain-text default is suitable.

Requests use normal TLS verification. Curl's default `.curlrc` is disabled to prevent hidden flags from changing behavior. The process still inherits its environment, including custom CA settings and, if allowed, proxies. Attempts are immediate and bounded; provider failures are retried up to the configured count, then the next discovery endpoint is tried. The monitor does not wait for long provider Retry-After periods; failed lookup is recorded and retried at the next due sample.

The `{ip}` substitution is made only after successful IP parsing, so it cannot insert shell commands or arbitrary URL characters. Commands are passed as argument arrays without invoking a shell. Enrichment transport does not force a family: for example, an IPv6 observation can be enriched by querying the provider over IPv4.

With `notify_on: [country_change]`, IP changes within one country are recorded in the log but do not send email. With both triggers, each qualifying observation sends one email; when both changed, it is presented as a country change. Country comparison uses the provider's `country_code`, and its sensitivity depends on `geoip.cache_ttl_minutes`: an IP change always triggers a fresh lookup, but a provider reassigning an unchanged IP is noticed only when the cache entry expires.

The `email` section is validated even when disabled, so a configuration can be prepared before it is switched on. A `password` key is rejected: secrets belong in `password_file` or `password_env`, not in YAML that might be copied or committed. SMTP always uses TLS with certificate and hostname verification and a TLS 1.2 minimum. With `starttls`, the tool stops before authentication if the server does not offer STARTTLS. `check` reads the password source and `ca_file` but makes no network connection; `test-email` performs a real delivery.

The GeoIP cache is keyed by IP and additionally checked against the endpoint. It holds at most 64 successful lookups. A changed IP gets its own lookup unless its prior cache entry is still valid. An expired cache is not presented as fresh data if the provider fails. Provider errors are not cached.
