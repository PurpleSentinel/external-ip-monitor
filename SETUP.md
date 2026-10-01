# Linux installation and operation

## 1. Install prerequisites

Debian/Ubuntu:

```bash
sudo apt-get update
sudo apt-get install python3 python3-venv curl
```

Fedora:

```bash
sudo dnf install python3 python3-pip curl
```

Cron must also be installed and running. On Fedora, if necessary:

```bash
sudo dnf install cronie
sudo systemctl enable --now crond
```

On Debian/Ubuntu, if necessary:

```bash
sudo apt-get install cron
sudo systemctl enable --now cron
```

Python needs access to a package index to install PyYAML and build tooling. Normal execution needs HTTPS access to the discovery and GeoIP endpoints. `openssl` is needed only for the local HTTPS integration tests.

## 2. Install the application

Clone the repository into a permanent directory owned by the user who will run cron, such as `$HOME/external-ip-monitor`. Keep it there: moving a Python virtualenv can break its executable paths.

From inside the cloned directory:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
cp config.example.yaml config.yaml
chmod 600 config.yaml
```

Installing with `pip install .` copies the application into the virtualenv. Re-run that command after changing source files. For development, use `pip install -e .` instead.

## 3. Configure

Edit `config.yaml`. All YAML options and defaults are in [docs/CONFIGURATION.md](docs/CONFIGURATION.md). The sample uses five minutes and IPv4. Paths resolve relative to the YAML directory regardless of the working directory.

The cron user needs write access to log, state and lock parent directories. Newly created data directories use mode 0700; new log, lock and state files use mode 0600. Existing log/directory permissions are not modified. Use a dedicated writable directory per configuration. Do not share a log, state or lock between independent configurations or users.

Validate and make the first observation:

```bash
.venv/bin/ipwatch check --config "$PWD/config.yaml"
.venv/bin/ipwatch run --config "$PWD/config.yaml" --force --stdout
```

`check` validates YAML syntax and values and, when email is enabled, the password file or variable and any `ca_file`; it does not verify output permissions, curl availability or network reachability. `--force` still respects the process lock and reserves the next interval. The live `config.yaml` and `var/` are listed in `.gitignore` because they contain your IP and location history. The first successful sample has `changed: null` because there is no previous observation.

## 4. Set up cron

Print the ready-to-paste line:

```bash
.venv/bin/ipwatch cron-line --config "$PWD/config.yaml"
crontab -e
```

Paste the printed line. An example, using illustrative absolute paths:

```cron
* * * * * /home/mark/external-ip-monitor/.venv/bin/python -m ipwatch run --config /home/mark/external-ip-monitor/config.yaml
```

This is a **user crontab** line. `/etc/cron.d` needs an additional username field and is not the format printed by this command. The tool quotes paths containing spaces and escapes cron's special percent characters.

Keep stderr visible through your cron implementation's error handling, or append an absolute error-file redirect:

```cron
* * * * * /home/mark/external-ip-monitor/.venv/bin/python -m ipwatch run --config /home/mark/external-ip-monitor/config.yaml 2>>/home/mark/external-ip-monitor/cron-errors.log
```

Normal runs produce no stdout. `--stdout` can be useful interactively; adding it to cron may produce mail for every sample on systems configured for cron mail.

The first cron run samples immediately if state does not exist. Later runs measure the interval from the last attempt's start time, with 30 seconds of slack: a run is due once `interval_minutes × 60 − 30` seconds have elapsed. Because cron fires on the minute, this keeps a seven-minute interval at seven minutes, including across an hour boundary, even when Python start-up time varies between runs. After a manual `--force` run partway through a minute, the next sample lands on the first cron tick at least `interval − 30 s` later. Cron delays or a lock held by a slow previous run can still make an interval longer; this is best-effort sampling, not a precise timer. Missed samples are not backfilled. On a backward clock adjustment, the next invocation samples immediately and establishes a new baseline. UTC timestamps and synchronized system time help interpret the history.

## 5. Read and rotate logs

```bash
tail -n 5 var/history.jsonl
```

Each line can be parsed independently. For example:

```bash
python3 -c 'import json,pathlib; [print(r["timestamp_utc"],r["ip"],r["changed"]) for r in map(json.loads,pathlib.Path("var/history.jsonl").open())]'
```

For long-term operation, configure logrotate. Adapt [examples/logrotate.conf](examples/logrotate.conf), replacing its path and user/group, then install it as `/etc/logrotate.d/ipwatch`. The application opens the log on each sample, so rename-based rotation works without a daemon signal. Avoid `copytruncate`, which can lose records during concurrent writes. Keep the lock and state files out of rotation.

## 6. IPv6 or two address families

For IPv6, use a separate YAML file:

```yaml
interval_minutes: 5
label: starlink-ipv6
network:
  family: ipv6
  endpoints:
    - https://api6.ipify.org
output:
  log_file: var/ipv6-history.jsonl
  state_file: var/ipv6-state.json
  lock_file: var/ipv6.lock
```

Generate a separate cron line for it. IPv4 and IPv6 are independent observations and may be made at slightly different times. Keep all three output paths distinct between configurations. IPv6 must be usable on the Linux host. The observed IPv6 address may belong to the host rather than the router, and host privacy addresses can change independently of the ISP's delegated prefix.

## 7. Email notifications (optional)

The tool can email you when the observed IP changes, when the GeoIP country changes, or both. Choose with `notify_on`:

| `notify_on` | Email when |
| --- | --- |
| `[ip_change]` (default) | The public IP differs from the last successful observation |
| `[country_change]` | The GeoIP country code differs from the last known country; IP changes within a country are logged but not emailed |
| `[ip_change, country_change]` | Either; a sample where both changed sends one email, headed as a country change |

Only changes trigger email: the first observation, unchanged samples and failed discoveries do not. Country detection needs GeoIP enabled, and reflects the provider's database, not a physical location. For example, Starlink addresses are often placed in the country of the ground-station point of presence or of registration, which can differ from where the dish is. If GeoIP fails when the IP changes, the country change is reported at the next successful lookup.

Store the SMTP password in a file only you can read. Most large providers require an app password rather than your normal login password.

```bash
mkdir -p ~/.config/ipwatch
( umask 077; printf '%s\n' 'YOUR-APP-PASSWORD' > ~/.config/ipwatch/smtp-password )
```

Typing the password on the command line leaves it in shell history; to avoid that, open the file in an editor instead and then run `chmod 600` on it. The tool refuses a password file that is group- or world-readable, not owned by you, or a symlink. Keep it outside the repository; as a safety net, `.gitignore` also excludes files named `smtp-password` or `smtp-password.*` and any `secrets/` directory. Alternatively, set `password_env` to the name of an environment variable; cron starts with a minimal environment, so the file is usually simpler.

Then enable the `email` section of `config.yaml`:

```yaml
email:
  enabled: true
  notify_on: [country_change]   # or [ip_change], or both
  smtp_host: smtp.gmail.com
  security: starttls              # port 587 by default; use tls for port 465
  username: you@gmail.com
  password_file: ~/.config/ipwatch/smtp-password
  from: ipwatch <you@gmail.com>
  to:
    - you@gmail.com
```

Validate, then send a test message:

```bash
.venv/bin/ipwatch check --config "$PWD/config.yaml"
.venv/bin/ipwatch test-email --config "$PWD/config.yaml"
```

Common settings follow. Providers change their policies, so confirm them in your provider's documentation.

| Provider | `smtp_host` | `security` / port | Notes |
| --- | --- | --- | --- |
| Gmail | `smtp.gmail.com` | `starttls` / 587 or `tls` / 465 | Needs 2-Step Verification and an app password |
| Fastmail | `smtp.fastmail.com` | `tls` / 465 | Needs an app password |
| iCloud Mail | `smtp.mail.me.com` | `starttls` / 587 | Needs an app-specific password; `username` is your iCloud address |
| Microsoft 365 / Outlook.com | `smtp.office365.com` | `starttls` / 587 | Microsoft is retiring password-based SMTP AUTH; it may be disabled for your account |
| Your own server or relay | its hostname | as configured | Use `ca_file` if it has a private CA certificate |

Plaintext SMTP, including an unencrypted local relay on port 25, is deliberately not supported. Certificate verification cannot be disabled; for a private CA, set `ca_file` to its PEM bundle.

How delivery behaves:

- The email is sent after the observation is appended to the log and the state is saved, while the run still holds the lock. SMTP adds up to `timeout_seconds` per network operation to that run.
- If delivery fails, the notice stays in `state.json` (`notify_pending`) and is retried at each later due sample that discovers an IP. The run exits with code 3 and writes the reason to stderr, so keep the cron stderr redirect from section 4. Changes that occur before delivery succeeds are queued in order and sent together in one email, so a later IP-only change cannot hide an earlier country change. The queue holds up to 50 changes; beyond that, the oldest IP-only changes are dropped first and the email says how many. The JSONL log keeps everything.
- If the server accepts some recipients and refuses others, the message is not resent; the refused addresses are reported on stderr.
- Messages contain the new and previous IP, host, label, time, and GeoIP location and ISP when available. Treat them as you treat the log.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| No new line | Check cron service, interval, lock contention, stderr and writable directories |
| `ip: null`, status `error` | Inspect `errors`; check connectivity, DNS, curl and endpoint availability |
| Status `partial` | IP discovery worked; check GeoIP provider availability/quota/schema |
| Wrong location | GeoIP is an estimate for the egress IP; verify VPN/routing and provider data |
| Wrong IP due to proxy | Default bypasses explicit proxy environment variables; VPN and transparent routing still apply |
| Wrong curl path | Set `network.curl_binary` to the output of `command -v curl` |
| TLS errors | Check the system CA store and network interception; do not disable verification |
| Exit code 3 / email not received | Run `ipwatch test-email`; check host, port, `security`, app password, and stderr. A queued notice retries at the next due sample |
| Certificate verification failed | Check `smtp_host` matches the server certificate; for a private CA set `ca_file`. Do not route around verification |
| Corrupt state | Stop cron briefly; preserve the damaged file, move it aside, then resume to create fresh state |

Resetting state removes the prior IP baseline, cache and interval history. Preserve the JSONL log to retain evidence. If changing network family, use a new state file so the prior family is not reported as an IP change.

Do not delete or replace a lock file while a run may hold it. Locks attach to an inode: replacing the file can allow two concurrent runs. A terminated process releases its lock automatically, so an old empty lock file does not mean the tool is stuck.

Maximum request duration is bounded per attempt. With the default single discovery endpoint and successful discovery, two discovery attempts plus two enrichment attempts can take roughly 68 seconds including process timeout margins. Additional discovery endpoints multiply the discovery budget. Overlapping cron invocations skip while the lock is held.
