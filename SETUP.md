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

Extract the archive into a permanent directory owned by the user who will run cron, such as `$HOME/external-ip-monitor`. Keep it there: moving a Python virtualenv can break its executable paths.

From inside the extracted directory:

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

`check` validates YAML syntax and values; it does not verify filesystem permissions, curl availability or network reachability. `--force` still respects the process lock and reserves the next interval. The first successful sample has `changed: null` because there is no previous observation.

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

The first cron run samples immediately if state does not exist. Later runs measure the interval from the last attempt's start time. An interval of seven minutes stays seven minutes across an hour boundary. Cron resolution and delays can make it longer; this is best-effort sampling, not a precise timer. Missed samples are not backfilled. On a backward clock adjustment, the next invocation samples immediately and establishes a new baseline. UTC timestamps and synchronized system time help interpret the history.

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
| Corrupt state | Stop cron briefly; preserve the damaged file, move it aside, then resume to create fresh state |

Resetting state removes the prior IP baseline, cache and interval history. Preserve the JSONL log to retain evidence. If changing network family, use a new state file so the prior family is not reported as an IP change.

Do not delete or replace a lock file while a run may hold it. Locks attach to an inode: replacing the file can allow two concurrent runs. A terminated process releases its lock automatically, so an old empty lock file does not mean the tool is stuck.

Maximum request duration is bounded per attempt. With the default single discovery endpoint and successful discovery, two discovery attempts plus two enrichment attempts can take roughly 68 seconds including process timeout margins. Additional discovery endpoints multiply the discovery budget. Overlapping cron invocations skip while the lock is held.
