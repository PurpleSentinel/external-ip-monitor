import dataclasses
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ipwatch.cli import cron_line, main
from ipwatch.config import ConfigError, load_config
from ipwatch.monitor import run
from ipwatch.storage import StorageError
from ipwatch.transport import CurlClient, RequestError


GEO = {"success": True, "ip": "8.8.8.8", "country": "United States", "country_code": "US",
       "city": "Example", "latitude": 37.4, "longitude": -122.1,
       "connection": {"asn": 15169, "isp": "Google", "org": "Google"},
       "timezone": {"id": "America/Los_Angeles"}}


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def get(self, url, family=None):
        self.calls.append((url, family))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.yaml = self.root / "config.yaml"
        self.yaml.write_text("interval_minutes: 7\n")
        self.config = load_config(self.yaml)

    def records(self):
        return [json.loads(line) for line in self.config.log_file.read_text().splitlines()]

    def test_interval_crosses_hour_and_force(self):
        c = dataclasses.replace(self.config, geo_enabled=False)
        first, _ = run(c, client=FakeClient(["8.8.8.8"]), now=3500)
        skipped, code = run(c, client=FakeClient([]), now=3919)
        self.assertIsNone(skipped)
        self.assertEqual(code, 0)
        run(c, client=FakeClient(["8.8.8.8"]), now=3920)
        run(c, client=FakeClient(["1.1.1.1"]), now=3921, force=True)
        records = self.records()
        self.assertEqual(len(records), 3)
        self.assertIsNone(first["changed"])
        self.assertFalse(records[1]["changed"])
        self.assertTrue(records[2]["changed"])
        self.assertEqual(records[2]["previous_ip"], "8.8.8.8")

    def test_failure_is_logged_and_throttled(self):
        record, code = run(self.config, client=FakeClient([RequestError("timeout")]), now=1000)
        self.assertEqual(code, 1)
        self.assertEqual(record["status"], "error")
        self.assertIsNone(record["ip"])
        self.assertEqual(record["geoip"]["status"], "not_attempted")
        self.assertIsNone(run(self.config, client=FakeClient([]), now=1001)[0])
        self.assertEqual(len(self.records()), 1)

    def test_failure_does_not_erase_last_success(self):
        c = dataclasses.replace(self.config, geo_enabled=False)
        run(c, client=FakeClient(["8.8.8.8"]), now=0)
        run(c, client=FakeClient([RequestError("offline")]), now=420)
        r, _ = run(c, client=FakeClient(["1.1.1.1"]), now=840)
        self.assertEqual(r["previous_ip"], "8.8.8.8")
        self.assertTrue(r["changed"])

    def test_geo_cache_and_ttl_expiry(self):
        c = dataclasses.replace(self.config, geo_cache_ttl_minutes=10)
        first, _ = run(c, client=FakeClient(["8.8.8.8", json.dumps(GEO)]), now=0)
        cached_client = FakeClient(["8.8.8.8"])
        cached, _ = run(c, client=cached_client, now=420)
        refreshed, _ = run(c, client=FakeClient(["8.8.8.8", json.dumps(GEO)]), now=840)
        self.assertEqual(first["geoip"]["data"]["asn"], 15169)
        self.assertTrue(cached["geoip"]["cache_hit"])
        self.assertEqual(len(cached_client.calls), 1)
        self.assertFalse(refreshed["geoip"]["cache_hit"])

    def test_zero_ttl_disables_reuse(self):
        c = dataclasses.replace(self.config, geo_cache_ttl_minutes=0)
        for t in (0, 420):
            client = FakeClient(["8.8.8.8", json.dumps(GEO)])
            r, _ = run(c, client=client, now=t)
            self.assertFalse(r["geoip"]["cache_hit"])
            self.assertEqual(len(client.calls), 2)

    def test_geo_failures_keep_ip_and_retry_next_sample(self):
        for response in [RequestError("curl exited with code 22"), "<html>",
                         '{"success":false}', '{"success":true,"ip":"1.1.1.1"}', "[]"]:
            with self.subTest(response=response):
                r, code = run(self.config, client=FakeClient(["8.8.8.8", response]), force=True, now=1000)
                self.assertEqual(code, 0)
                self.assertEqual(r["status"], "partial")
                self.assertEqual(r["ip"], "8.8.8.8")
                self.assertEqual(r["geoip"]["status"], "error")
        r, _ = run(self.config, client=FakeClient(["8.8.8.8", json.dumps(GEO)]), force=True, now=1001)
        self.assertEqual(r["status"], "ok")

    def test_fallback_validates_ip(self):
        c = dataclasses.replace(self.config, geo_enabled=False,
            endpoints=("https://one.example", "https://two.example"))
        r, code = run(c, client=FakeClient(["100.64.1.1", "8.8.8.8\n"]), now=0)
        self.assertEqual(code, 0)
        self.assertEqual(r["ip_source"], "https://two.example")
        self.assertEqual(len(r["errors"]), 1)

    def test_nonpublic_invalid_wrong_family_rejected(self):
        for ip in ["127.0.0.1", "192.168.1.1", "100.64.1.1", "<html>",
                   "2001:4860:4860::8888", "224.0.0.1", "8.8.8.8\n1.1.1.1"]:
            with self.subTest(ip=ip):
                r, code = run(self.config, client=FakeClient([ip]), force=True, now=0)
                self.assertIsNone(r["ip"])
                self.assertEqual(code, 1)

    def test_ipv6(self):
        self.yaml.write_text("network:\n  family: ipv6\ngeoip:\n  enabled: false\n")
        c = load_config(self.yaml)
        client = FakeClient(["2001:4860:4860:0000:0000:0000:0000:8888"])
        r, code = run(c, client=client, now=0)
        self.assertEqual(r["ip"], "2001:4860:4860::8888")
        self.assertEqual(client.calls[0], ("https://api6.ipify.org", "ipv6"))
        self.assertEqual(code, 0)

    def test_concurrent_process_skips_under_lock(self):
        self.config.lock_file.parent.mkdir()
        with self.config.lock_file.open("w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run([sys.executable, "-m", "ipwatch", "run", "--config", str(self.yaml)],
                capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(self.config.state_file.exists())
            self.assertFalse(self.config.log_file.exists())

    def test_corrupt_state_refuses_to_overwrite(self):
        self.config.state_file.parent.mkdir()
        self.config.state_file.write_text("broken")
        with self.assertRaises(StorageError):
            run(self.config, client=FakeClient([]), now=0)
        self.assertEqual(self.config.state_file.read_text(), "broken")
        self.assertFalse(self.config.log_file.exists())

    def test_rollback_clock_samples(self):
        c = dataclasses.replace(self.config, geo_enabled=False)
        run(c, client=FakeClient(["8.8.8.8"]), now=1000)
        r, _ = run(c, client=FakeClient(["8.8.8.8"]), now=900)
        self.assertIsNotNone(r)

    def test_jsonl_single_line_and_permissions(self):
        c = dataclasses.replace(self.config, label='name"slash\\', geo_enabled=False)
        r, _ = run(c, client=FakeClient(["8.8.8.8"]), now=0)
        raw = c.log_file.read_text()
        self.assertEqual(raw.count("\n"), 1)
        self.assertEqual(json.loads(raw), r)
        self.assertEqual(c.log_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(c.state_file.stat().st_mode & 0o777, 0o600)

    def test_failed_log_write_preserves_last_success(self):
        c = dataclasses.replace(self.config, geo_enabled=False)
        run(c, client=FakeClient(["8.8.8.8"]), now=0)
        with patch("ipwatch.monitor.append_record", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                run(c, client=FakeClient(["1.1.1.1"]), now=420)
        state = json.loads(c.state_file.read_text())
        self.assertEqual(state["last_success_ip"], "8.8.8.8")
        self.assertEqual(state["last_attempt_epoch"], 420)

    def test_config_validation(self):
        for data in ["interval_minutes: 0", "interval_minutes: true", "interval_minutes: 1.5",
                     "interval_minutes: 5\ninterval_minutes: 10", "[a]: 1",
                     "interval_minute: 5", "network: []", "output: {log_file: ''}",
                     "network: {endpoints: ['http://example.com']}",
                     "network: {endpoints: ['https://user:pass@example.com']}",
                     "network: {attempts: 0}", "geoip: {enabled: 'yes'}",
                     "geoip: {endpoint: 'https://example.com'}",
                     "output: {log_file: same, state_file: same}",
                     "!!python/object/apply:os.system ['echo nope']"]:
            with self.subTest(data=data):
                self.yaml.write_text(data)
                with self.assertRaises(ConfigError):
                    load_config(self.yaml)

    def test_relative_paths_and_check_no_side_effects(self):
        c = load_config(self.yaml)
        self.assertEqual(c.log_file, self.root / "var/history.jsonl")
        with patch("builtins.print"):
            self.assertEqual(main(["check", "--config", str(self.yaml)]), 0)
        self.assertFalse((self.root / "var").exists())

    def test_cron_line_quotes_spaces_and_percent(self):
        renamed = self.root / "my config%1.yaml"
        renamed.write_text("interval_minutes: 90\n")
        line = cron_line(load_config(renamed))
        self.assertTrue(line.startswith("* * * * * "))
        self.assertIn("\\%1.yaml'", line)
        self.assertIn("-m ipwatch run --config", line)

    def test_cache_bound_and_changed_provider(self):
        run(self.config, client=FakeClient(["8.8.8.8", json.dumps(GEO)]), now=0)
        state = json.loads(self.config.state_file.read_text())
        state["geo_cache"] = {str(i): {} for i in range(64)}
        self.config.state_file.write_text(json.dumps(state))
        run(self.config, client=FakeClient(["8.8.8.8", json.dumps(GEO)]), now=420)
        self.assertEqual(len(json.loads(self.config.state_file.read_text())["geo_cache"]), 64)
        c = dataclasses.replace(self.config, geo_endpoint="https://new.example/{ip}")
        client = FakeClient(["8.8.8.8", json.dumps(GEO)])
        run(c, client=client, force=True, now=421)
        self.assertEqual(len(client.calls), 2)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        yaml = self.root / "config.yaml"
        yaml.write_text("network: {attempts: 1}\n")
        self.config = load_config(yaml)

    def test_real_subprocess_argv_body_and_no_shell(self):
        # Exercise the curl process boundary with a fake executable, no network.
        fake = self.root / "fake-curl"
        argsfile = self.root / "args.json"
        fake.write_text('#!' + sys.executable + '\nimport sys,json\n'
            + f'open({str(argsfile)!r},"w").write(json.dumps(sys.argv))\n'
            + 'open(sys.argv[sys.argv.index("--output")+1],"w").write("8.8.8.8\\n")\n'
            + 'print("200",end="")\n')
        fake.chmod(0o700)
        c = dataclasses.replace(self.config, curl_binary=str(fake), interface="eth0; touch nothing")
        self.assertEqual(CurlClient(c).get("https://example.com", "ipv4"), "8.8.8.8\n")
        args = json.loads(argsfile.read_text())
        self.assertEqual(args[1], "--disable")
        self.assertIn("eth0; touch nothing", args)
        self.assertIn("--ipv4", args)
        self.assertIn("--noproxy", args)
        self.assertNotIn("--insecure", args)
        self.assertFalse((self.root / "nothing").exists())

    def test_missing_curl(self):
        c = dataclasses.replace(self.config, curl_binary=str(self.root / "missing"))
        with self.assertRaises(RequestError):
            CurlClient(c).get("https://example.com")

    def test_curl_retry_and_http_failure(self):
        c = dataclasses.replace(self.config, attempts=2)
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 22, b"403", b"secret")) as proc:
            with self.assertRaisesRegex(RequestError, "code 22"):
                CurlClient(c).get("https://example.com")
            self.assertEqual(proc.call_count, 2)
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"302", b"")):
            with self.assertRaisesRegex(RequestError, "HTTP status"):
                CurlClient(self.config).get("https://example.com")

    def test_process_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("curl", 17)):
            with self.assertRaisesRegex(RequestError, "timeout"):
                CurlClient(self.config).get("https://example.com")

    def test_response_size_limit(self):
        def response(cmd, **kwargs):
            fd = int(cmd[cmd.index("--output") + 1].rsplit("/", 1)[1])
            os.write(fd, b"x" * 65537)
            return subprocess.CompletedProcess(cmd, 0, b"200", b"")
        with patch("subprocess.run", side_effect=response):
            with self.assertRaisesRegex(RequestError, "64 KiB"):
                CurlClient(self.config).get("https://example.com")


if __name__ == "__main__":
    unittest.main()
