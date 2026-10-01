# JSON Lines record format, schema version 1

Every due invocation that completes processing appends one UTF-8 JSON object and one newline. Keys use snake_case. Records include successful, unchanged, changed, partial and failed observations. UTC timestamps use ISO 8601 with `Z`; their resolution is one second. The record timestamp is the attempt's start, not the precise instant the provider processed the request.

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | integer | `1` |
| `event` | string | `external_ip_observation` |
| `record_id` | string | UUID identifying the record |
| `timestamp_utc` | string | Start of attempt in UTC |
| `hostname` | string | Local hostname |
| `label` | string or null | User's configured connection name |
| `interval_minutes` | integer | Configured interval at observation time |
| `family` | string | `ipv4` or `ipv6` |
| `status` | string | `ok`, `partial`, or `error` |
| `ip` | string or null | Validated and canonicalized public IP; null on discovery failure |
| `previous_ip` | string or null | Last successful observed IP, even if intervening attempts failed |
| `changed` | boolean or null | Comparison to previous successful IP; null without a baseline or when discovery failed |
| `ip_source` | string or null | Discovery endpoint that returned the accepted IP |
| `geoip` | object | Enrichment details |
| `errors` | array | Failure messages, possibly including failed endpoints before successful fallback |

A successful fallback can have `status: ok` and nonempty `errors`. `partial` means an IP was obtained but enabled enrichment failed. Disabled enrichment does not make a record partial. Partial observations update the successful IP baseline. Failure to discover an IP leaves that baseline intact.

## GeoIP object

| Field | Type | Description |
| --- | --- | --- |
| `status` | string | `ok`, `error`, `disabled`, or `not_attempted` |
| `provider` | string or null | `ipwhois` identifies the response-schema adapter, even for a compatible custom endpoint |
| `cache_hit` | boolean | True when successful enrichment was reused from state |
| `lookup_at_utc` | string or null | Original lookup time, retained when using cache |
| `data` | object or null | Normalized selected fields; null on error/disabled/not attempted |

`data` contains `continent`, `country`, `country_code`, `region`, `city`, `latitude`, `longitude`, `timezone`, `asn`, `isp`, and `organization`. All can be null if missing or of an unsupported type. Coordinates are numbers with latitude constrained to -90 to 90 and longitude to -180 to 180. ASN is an integer when supplied as such by the provider. Additional provider response fields are not stored. The returned IP must match the requested IP.

Each error contains `stage` (`ip_discovery` or `geoip`) and a `message` string. Raw HTTP bodies and curl stderr are not copied into errors. Records can still contain IPs, location estimates, endpoint URLs, hostname and label: protect and retain them appropriately for your intended use.

## Failure and durability boundaries

The attempt timestamp is persisted before requests, including attempts made while offline. Disk/configuration errors are reported to stderr with exit code 2 rather than pretending a record was written. A process killed after reservation but before append can leave a gap until the next interval. No historical samples are invented.

Append and state update are separate filesystem operations. They are flushed and fsynced, but there is no transaction across the two files. A crash after appending and before the final state update can leave a record whose new IP is not yet reflected in the baseline. A power failure during append can leave a partial final line. Preserve the original file and repair only that tail before resuming if required. JSONL logging is operational history, not a tamper-evident evidence store; signing, remote storage and evidential chain of custody are future features.

A skipped, not-due or locked invocation does not emit a record. Missed cron executions do not generate backfill. GeoIP refreshes happen only on due observations.
