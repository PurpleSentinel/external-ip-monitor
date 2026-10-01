import dataclasses
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ipwatch.cli import main
from ipwatch.config import ConfigError, load_config
from ipwatch.monitor import run
from ipwatch.notify import NotifyError, compose, read_password
from ipwatch.transport import RequestError


# Fictional values: XA/XB are ISO 3166 user-assigned codes; ASN 64500 is reserved for documentation.
GEO = {"success": True, "ip": "1.1.1.1", "country": "Example Country", "country_code": "XA",
       "city": "Example City", "connection": {"asn": 64500, "isp": "Example ISP"}}

EMAIL_YAML = """interval_minutes: 5
label: example-home
email:
  enabled: true
  smtp_host: smtp.example.com
  username: me@example.com
  password_file: secret.txt
  from: ipwatch <ipwatch@example.com>
  to: [me@example.com]
"""


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)

    def get(self, url, family=None):
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeMailer:
    def __init__(self, failures=0):
        self.failures = failures
        self.sent = []

    def __call__(self, email, msg):
        if self.failures:
            self.failures -= 1
            raise NotifyError("SMTP delivery failed: ConnectionRefusedError")
        self.sent.append(msg)
        return []


class EmailConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.yaml = self.root / "config.yaml"

    def load(self, text):
        self.yaml.write_text(text)
        return load_config(self.yaml)

    def test_defaults_and_relative_paths(self):
        email = self.load(EMAIL_YAML).email
        self.assertEqual((email.security, email.port, email.timeout_seconds), ("starttls", 587, 20))
        self.assertEqual(email.password_file, self.root / "secret.txt")
        self.assertEqual(email.recipients, ("me@example.com",))
        tls = self.load(EMAIL_YAML.replace("  enabled: true\n", "  enabled: true\n  security: tls\n")).email
        self.assertEqual(tls.port, 465)

    def test_disabled_or_absent_section(self):
        self.assertIsNone(self.load("interval_minutes: 5\n").email)
        self.assertIsNone(self.load(EMAIL_YAML.replace("enabled: true", "enabled: false")).email)
        with self.assertRaises(ConfigError):  # disabled sections are still validated
            self.load(EMAIL_YAML.replace("enabled: true", "enabled: false\n  security: none"))

    def test_rejected_settings(self):
        cases = {
            "plaintext": ("  enabled: true\n", "  enabled: true\n  security: none\n"),
            "inline password": ("  password_file: secret.txt\n", "  password: hunter2\n"),
            "two password sources": ("  password_file: secret.txt\n",
                                     "  password_file: secret.txt\n  password_env: SMTP_PASSWORD\n"),
            "no password source": ("  password_file: secret.txt\n", ""),
            "bad env name": ("  password_file: secret.txt\n", "  password_env: 'A B'\n"),
            "two addresses in one": ("[me@example.com]", "['a@example.com, b@example.com']"),
            "not an address": ("[me@example.com]", "[nobody]"),
            "no recipients": ("[me@example.com]", "[]"),
            "missing host": ("  smtp_host: smtp.example.com\n", ""),
            "host with path": ("smtp.example.com", "smtp.example.com/x"),
            "bad port": ("  enabled: true\n", "  enabled: true\n  smtp_port: 0\n"),
            "unknown key": ("  enabled: true\n", "  enabled: true\n  verify: false\n"),
            "password is config": ("password_file: secret.txt", "password_file: config.yaml"),
        }
        for name, (old, new) in cases.items():
            with self.subTest(name):
                self.assertIn(old, EMAIL_YAML)
                with self.assertRaises(ConfigError):
                    self.load(EMAIL_YAML.replace(old, new))

    def test_notify_on(self):
        self.assertEqual(self.load(EMAIL_YAML).email.notify_on, ("ip_change",))
        both = self.load(EMAIL_YAML.replace("  enabled: true\n",
            "  enabled: true\n  notify_on: [country_change, ip_change]\n")).email
        self.assertEqual(both.notify_on, ("country_change", "ip_change"))
        for bad in ("[]", "[ip]", "[ip_change, ip_change]", "country_change", "[city_change]"):
            with self.subTest(bad), self.assertRaises(ConfigError):
                self.load(EMAIL_YAML.replace("  enabled: true\n", f"  enabled: true\n  notify_on: {bad}\n"))
        with self.assertRaisesRegex(ConfigError, "requires geoip.enabled"):
            self.load(EMAIL_YAML.replace("  enabled: true\n", "  enabled: true\n  notify_on: [country_change]\n")
                      + "geoip:\n  enabled: false\n")

    def test_inline_password_error_is_explicit(self):
        with self.assertRaisesRegex(ConfigError, "password_file or email.password_env"):
            self.load(EMAIL_YAML.replace("  password_file: secret.txt\n", "  password: hunter2\n"))


class PasswordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "config.yaml").write_text(EMAIL_YAML)
        self.email = load_config(self.root / "config.yaml").email
        self.secret = self.root / "secret.txt"

    def test_file_mode_and_first_line(self):
        self.secret.write_text("s3cret pass\nignored\n")
        self.secret.chmod(0o644)
        with self.assertRaisesRegex(NotifyError, "0600"):
            read_password(self.email)
        self.secret.chmod(0o600)
        self.assertEqual(read_password(self.email), "s3cret pass")

    def test_missing_empty_and_symlink(self):
        with self.assertRaises(NotifyError):
            read_password(self.email)
        self.secret.write_text("\n")
        self.secret.chmod(0o600)
        with self.assertRaisesRegex(NotifyError, "empty"):
            read_password(self.email)
        target = self.root / "real.txt"
        target.write_text("pw")
        target.chmod(0o600)
        self.secret.unlink()
        self.secret.symlink_to(target)
        with self.assertRaises(NotifyError):
            read_password(self.email)

    def test_environment_variable(self):
        email = dataclasses.replace(self.email, password_file=None, password_env="IPWATCH_TEST_PW")
        with patch.dict(os.environ, {"IPWATCH_TEST_PW": "from-env"}):
            self.assertEqual(read_password(email), "from-env")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(NotifyError):
                read_password(email)

    def test_missing_ca_file_is_a_notify_error(self):
        from ipwatch.notify import tls_context
        with self.assertRaisesRegex(NotifyError, "ca_file"):
            tls_context(dataclasses.replace(self.email, ca_file=self.root / "missing.pem"))

    def test_check_command_reports_bad_password_file_as_config_error(self):
        with patch("sys.stderr"), patch("builtins.print"):
            self.assertEqual(main(["check", "--config", str(self.root / "config.yaml")]), 2)
        self.secret.write_text("pw\n")
        self.secret.chmod(0o600)
        with patch("builtins.print"):
            self.assertEqual(main(["check", "--config", str(self.root / "config.yaml")]), 0)


class NotificationFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "config.yaml").write_text(EMAIL_YAML)
        self.config = load_config(self.root / "config.yaml")

    def state(self):
        return json.loads(self.config.state_file.read_text())

    def records(self):
        return [json.loads(line) for line in self.config.log_file.read_text().splitlines()]

    def test_only_changes_send_email(self):
        mailer = FakeMailer()
        c = dataclasses.replace(self.config, geo_enabled=False)
        run(c, client=FakeClient(["8.8.8.8"]), now=0, mailer=mailer)         # first sample
        run(c, client=FakeClient(["8.8.8.8"]), now=300, mailer=mailer)       # unchanged
        run(c, client=FakeClient(["8.8.8.8"]), now=600, mailer=mailer)       # unchanged
        self.assertEqual(mailer.sent, [])
        record, code = run(c, client=FakeClient(["1.1.1.1"]), now=900, mailer=mailer)
        self.assertEqual(code, 0)
        self.assertEqual(len(mailer.sent), 1)
        msg = mailer.sent[0]
        self.assertIn("8.8.8.8", msg.get_content())
        self.assertIn("changed to 1.1.1.1", msg["Subject"])
        self.assertIn(record["record_id"], msg.get_content())
        self.assertIsNone(self.state()["notify_pending"])

    def test_email_disabled_never_queues(self):
        mailer = FakeMailer()
        c = dataclasses.replace(self.config, geo_enabled=False, email=None)
        run(c, client=FakeClient(["8.8.8.8"]), now=0, mailer=mailer)
        run(c, client=FakeClient(["1.1.1.1"]), now=300, mailer=mailer)
        self.assertEqual(mailer.sent, [])
        self.assertIsNone(self.state()["notify_pending"])

    def test_failure_is_queued_and_retried_on_next_due_sample(self):
        mailer = FakeMailer(failures=2)
        c = dataclasses.replace(self.config, geo_enabled=False)
        run(c, client=FakeClient(["8.8.8.8"]), now=0, mailer=mailer)
        with patch("sys.stderr") as stderr:
            record, code = run(c, client=FakeClient(["1.1.1.1"]), now=300, mailer=mailer)
            self.assertEqual(code, 3)
            self.assertIn("will retry", "".join(call.args[0] for call in stderr.write.call_args_list))
        # The observation itself is durable before any email is attempted.
        self.assertEqual(self.records()[-1]["record_id"], record["record_id"])
        pending = self.state()["notify_pending"]
        self.assertEqual((pending["attempts"], pending["notices"][0]["ip"]), (1, "1.1.1.1"))
        self.assertIsNone(run(c, client=FakeClient([]), now=360, mailer=mailer)[0])  # not due: no retry
        with patch("sys.stderr"):
            self.assertEqual(run(c, client=FakeClient(["1.1.1.1"]), now=600, mailer=mailer)[1], 3)
        _, code = run(c, client=FakeClient(["1.1.1.1"]), now=900, mailer=mailer)
        self.assertEqual(code, 0)
        self.assertEqual(len(mailer.sent), 1)
        body = mailer.sent[0].get_content()
        self.assertIn("2 earlier attempt(s) failed", body)
        self.assertIn("Previous IP:  8.8.8.8", body)
        self.assertIsNone(self.state()["notify_pending"])

    def test_no_retry_while_ip_discovery_fails(self):
        mailer = FakeMailer(failures=1)
        c = dataclasses.replace(self.config, geo_enabled=False)
        run(c, client=FakeClient(["8.8.8.8"]), now=0, mailer=mailer)
        with patch("sys.stderr"):
            run(c, client=FakeClient(["1.1.1.1"]), now=300, mailer=mailer)
        _, code = run(c, client=FakeClient([RequestError("offline")]), now=600, mailer=mailer)
        self.assertEqual(code, 1)
        self.assertEqual(self.state()["notify_pending"]["attempts"], 1)

    def test_old_state_without_pending_field_loads(self):
        self.config.state_file.parent.mkdir()
        self.config.state_file.write_text(json.dumps({"schema_version": 1, "last_attempt_epoch": 0,
            "last_success_ip": "8.8.8.8", "geo_cache": {}}))
        mailer = FakeMailer()
        c = dataclasses.replace(self.config, geo_enabled=False)
        _, code = run(c, client=FakeClient(["1.1.1.1"]), now=300, mailer=mailer)
        self.assertEqual((code, len(mailer.sent)), (0, 1))

    def test_message_content_with_geoip(self):
        mailer = FakeMailer()
        run(self.config, client=FakeClient(["8.8.8.8", json.dumps(GEO | {"ip": "8.8.8.8"})]), now=0, mailer=mailer)
        run(self.config, client=FakeClient(["1.1.1.1", json.dumps(GEO)]), now=300, mailer=mailer)
        msg = mailer.sent[0]
        body = msg.get_content()
        self.assertEqual(msg["From"], "ipwatch <ipwatch@example.com>")
        self.assertEqual(msg["To"], "me@example.com")
        self.assertTrue(msg["Subject"].startswith("[ipwatch] example-home: external IPv4 changed"))
        self.assertTrue(msg["Message-ID"].endswith("@example.com>"))
        self.assertIn("Example City, Example Country (GeoIP estimate)", body)
        self.assertIn("Country:      XA (previous XA)", body)
        self.assertIn("AS64500 Example ISP", body)

    def test_provider_control_characters_do_not_reach_message(self):
        notice = {"record_id": "r", "timestamp_utc": "t", "hostname": "h", "label": None, "family": "ipv6",
            "ip": "2001:db8::1", "previous_ip": "2001:db8::2", "changed": True, "ip_source": "https://api6.ipify.org",
            "country_code": None, "previous_country_code": None, "country_changed": False,
            "geoip": {"city": "Evil\r\nBcc: x@example.com", "country": "X", "asn": None, "isp": None}}
        msg = compose(self.config.email, {"attempts": 0, "last_error": None, "notices": [notice]})
        self.assertNotIn("\r\nBcc", msg.as_string())
        self.assertIsNone(msg["Bcc"])
        self.assertIn("external IPv6 changed to 2001:db8::1", msg["Subject"])
        # Country codes come from the provider and reach the Subject header.
        notice |= {"country_code": "XB\r\nBcc: x@example.com", "previous_country_code": "XA", "country_changed": True}
        msg = compose(self.config.email, {"attempts": 0, "last_error": None, "notices": [notice]})
        self.assertIsNone(msg["Bcc"])
        self.assertIn("country changed XA -> XBBcc", msg["Subject"])


def geo(ip, country_code, country="Somewhere"):
    return json.dumps({"success": True, "ip": ip, "country": country, "country_code": country_code})


class CountryChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "config.yaml").write_text(EMAIL_YAML)
        base = load_config(self.root / "config.yaml")
        # Zero TTL: every sample performs a lookup, so each test controls the country seen.
        self.base = dataclasses.replace(base, geo_cache_ttl_minutes=0)

    def config(self, *triggers):
        return dataclasses.replace(self.base, email=dataclasses.replace(self.base.email, notify_on=triggers))

    def sample(self, config, ip, geo_reply, now, mailer):
        return run(config, client=FakeClient([ip, geo_reply]), now=now, mailer=mailer)

    def test_record_country_fields_and_baseline(self):
        c, mailer = self.config("ip_change"), FakeMailer()
        first, _ = self.sample(c, "8.8.8.8", geo("8.8.8.8", "XA"), 0, mailer)
        self.assertEqual((first["country_code"], first["previous_country_code"], first["country_changed"]),
                         ("XA", None, None))
        same, _ = self.sample(c, "8.8.8.8", geo("8.8.8.8", "XA"), 300, mailer)
        self.assertEqual((same["previous_country_code"], same["country_changed"]), ("XA", False))
        # A failed lookup leaves the country unknown and the baseline intact...
        failed, _ = self.sample(c, "1.1.1.1", "<html>", 600, mailer)
        self.assertEqual((failed["country_code"], failed["country_changed"]), (None, None))
        # ...so the change is detected at the next successful lookup, not missed.
        later, _ = self.sample(c, "1.1.1.1", geo("1.1.1.1", "XB"), 900, mailer)
        self.assertEqual((later["previous_country_code"], later["country_changed"], later["changed"]),
                         ("XA", True, False))

    def test_country_only_ignores_ip_changes_within_a_country(self):
        c, mailer = self.config("country_change"), FakeMailer()
        self.sample(c, "8.8.8.8", geo("8.8.8.8", "XA"), 0, mailer)
        self.sample(c, "1.1.1.1", geo("1.1.1.1", "XA"), 300, mailer)
        self.assertEqual(mailer.sent, [])
        record, code = self.sample(c, "9.9.9.9", geo("9.9.9.9", "XB", "United Kingdom"), 600, mailer)
        self.assertEqual((code, len(mailer.sent)), (0, 1))
        msg = mailer.sent[0]
        self.assertEqual(msg["Subject"], "[ipwatch] example-home: country changed XA -> XB (IPv4 9.9.9.9)")
        self.assertIn("Country changed: XA -> XB", msg.get_content())
        self.assertIn(record["record_id"], msg.get_content())

    def test_country_change_without_ip_change(self):
        # Provider databases can move an unchanged IP to another country.
        for triggers, expected in ((("ip_change",), 0), (("country_change",), 1), (("ip_change", "country_change"), 1)):
            with self.subTest(triggers):
                for f in (self.base.state_file, self.base.log_file):
                    f.unlink(missing_ok=True)
                c, mailer = self.config(*triggers), FakeMailer()
                self.sample(c, "8.8.8.8", geo("8.8.8.8", "XA"), 0, mailer)
                self.sample(c, "8.8.8.8", geo("8.8.8.8", "XB"), 300, mailer)
                self.assertEqual(len(mailer.sent), expected)
                if expected:
                    self.assertIn("IP:           8.8.8.8 (unchanged)", mailer.sent[0].get_content())

    def test_both_triggers_send_one_email_per_change(self):
        c, mailer = self.config("ip_change", "country_change"), FakeMailer()
        self.sample(c, "8.8.8.8", geo("8.8.8.8", "XA"), 0, mailer)
        self.sample(c, "1.1.1.1", geo("1.1.1.1", "XA"), 300, mailer)  # IP change
        self.sample(c, "9.9.9.9", geo("9.9.9.9", "XB"), 600, mailer)  # IP and country change
        self.assertEqual(len(mailer.sent), 2)
        self.assertIn("external IPv4 changed to 1.1.1.1", mailer.sent[0]["Subject"])
        self.assertIn("country changed XA -> XB", mailer.sent[1]["Subject"])

    def test_queued_country_change_is_not_hidden_by_later_ip_change(self):
        c, mailer = self.config("ip_change", "country_change"), FakeMailer(failures=1)
        self.sample(c, "8.8.8.8", geo("8.8.8.8", "XA"), 0, mailer)
        with patch("sys.stderr"):
            _, code = self.sample(c, "9.9.9.9", geo("9.9.9.9", "XB"), 300, mailer)  # delivery fails
        self.assertEqual(code, 3)
        self.sample(c, "1.1.1.1", geo("1.1.1.1", "XB"), 600, mailer)  # IP-only change; retry succeeds
        self.assertEqual(len(mailer.sent), 1)
        msg = mailer.sent[0]
        self.assertEqual(msg["Subject"], "[ipwatch] example-home: country changed XA -> XB (IPv4 9.9.9.9) [2 changes]")
        body = msg.get_content()
        self.assertLess(body.index("Country changed: XA -> XB"), body.index("External IPv4 address changed"))
        self.assertIn("1 earlier attempt(s) failed", body)

    def test_queue_overflow_drops_ip_only_notices_first(self):
        from ipwatch.monitor import MAX_QUEUED_NOTICES, queue_notice
        state = {"notify_pending": None}
        record = {"record_id": "r", "timestamp_utc": "t", "hostname": "h", "label": None, "family": "ipv4",
            "ip": "8.8.8.8", "previous_ip": "1.1.1.1", "changed": True, "ip_source": "s",
            "country_code": "XB", "previous_country_code": "XA", "geoip": {"data": None}}
        queue_notice(state, record | {"record_id": "country", "country_changed": True})
        for i in range(MAX_QUEUED_NOTICES):
            queue_notice(state, record | {"record_id": f"ip{i}", "country_changed": False})
        notices = state["notify_pending"]["notices"]
        self.assertEqual(len(notices), MAX_QUEUED_NOTICES)
        self.assertEqual(notices[0]["record_id"], "country")
        self.assertEqual(state["notify_pending"]["dropped"], 1)
        self.assertIn("1 older IP-only change(s) were dropped",
                      compose(self.base.email, state["notify_pending"]).get_content())


if __name__ == "__main__":
    unittest.main()
