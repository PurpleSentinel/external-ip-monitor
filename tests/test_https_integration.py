"""Real curl + HTTPS + CLI integration; entirely local and independent of providers."""
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from ipwatch.config import load_config
from ipwatch.transport import CurlClient, RequestError


@unittest.skipUnless(shutil.which("curl") and shutil.which("openssl"), "curl and openssl required")
class HTTPSIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.cert = cls.root / "cert.pem"
        key = cls.root / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-keyout", str(key), "-out", str(cls.cert), "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost"], check=True, capture_output=True)
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/ip":
                    status, payload = 200, b"8.8.8.8\n"
                elif self.path == "/geo/8.8.8.8":
                    status, payload = 200, json.dumps({"success": True, "ip": "8.8.8.8",
                        "country": "Example country", "connection": {"asn": 15169}}).encode()
                else:
                    status, payload = 503, b"unavailable"
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *args):
                pass
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cls.cert, key)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"https://localhost:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.temp.cleanup()

    def setUp(self):
        self.case_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.case_temp.cleanup)
        self.case_root = Path(self.case_temp.name)
        self.yaml = self.case_root / "config.yaml"
        self.yaml.write_text(f"interval_minutes: 90\nnetwork:\n  curl_binary: {shutil.which('curl')}\n"
            f"  attempts: 1\n  endpoints: ['{self.base}/ip']\n"
            f"geoip:\n  endpoint: '{self.base}/geo/{{ip}}'\n")

    def test_full_cli_with_real_curl_and_geoip(self):
        env = dict(os.environ, CURL_CA_BUNDLE=str(self.cert))
        cmd = [sys.executable, "-m", "ipwatch", "run", "--config", str(self.yaml), "--stdout"]
        first = subprocess.run(cmd, capture_output=True, text=True, timeout=10, env=env)
        self.assertEqual(first.returncode, 0, first.stderr)
        record = json.loads(first.stdout)
        self.assertEqual(record["ip"], "8.8.8.8")
        self.assertEqual(record["geoip"]["data"]["asn"], 15169)
        self.assertEqual(record["status"], "ok")
        second = subprocess.run(cmd, capture_output=True, text=True, timeout=10, env=env)
        self.assertEqual(second.stdout, "")
        self.assertEqual(second.returncode, 0, second.stderr)
        third = subprocess.run(cmd + ["--force"], capture_output=True, text=True, timeout=10, env=env)
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertFalse(json.loads(third.stdout)["changed"])
        self.assertTrue(json.loads(third.stdout)["geoip"]["cache_hit"])
        lines = (self.case_root / "var/history.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0]), record)

    def test_real_http_failure(self):
        with patch.dict(os.environ, {"CURL_CA_BUNDLE": str(self.cert)}):
            with self.assertRaisesRegex(RequestError, "code 22"):
                CurlClient(load_config(self.yaml)).get(self.base + "/unavailable")

    def test_tls_verification_enforced(self):
        with patch.dict(os.environ, {"CURL_CA_BUNDLE": str(self.root / 'nonexistent.pem')}):
            with self.assertRaises(RequestError):
                CurlClient(load_config(self.yaml)).get(self.base + "/ip")
