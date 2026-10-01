"""Real smtplib + TLS against a local SMTP server; entirely local and independent of providers."""
import base64
import os
from pathlib import Path
import shutil
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest

from ipwatch.config import load_config
from ipwatch.notify import NotifyError, compose_test, send


def make_cert(directory, name):
    cert, key = directory / f"{name}.pem", directory / f"{name}-key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
        "-addext", "subjectAltName=DNS:localhost"], check=True, capture_output=True)
    return cert, key


class SMTPHandler(socketserver.BaseRequestHandler):
    """Just enough ESMTP: EHLO, STARTTLS, AUTH PLAIN, MAIL, RCPT, DATA, QUIT."""

    def handle(self):
        self.open = []
        try:
            self.converse()
        except (OSError, ssl.SSLError):
            pass  # Client aborted, e.g. after rejecting the certificate.
        finally:
            for item in reversed(self.open):
                item.close()

    def converse(self):
        server, sock, tls = self.server, self.request, self.server.implicit_tls
        if tls:
            sock = server.context.wrap_socket(sock, server_side=True)
            self.open.append(sock)
        reader = sock.makefile("rb")
        self.open.append(reader)
        reply = lambda line: sock.sendall(line.encode() + b"\r\n")
        reply("220 localhost ESMTP test")
        while line := reader.readline():
            command = line.decode().rstrip("\r\n")
            verb = command.split(" ", 1)[0].upper()
            if verb == "EHLO":
                lines = ["localhost"] + (["STARTTLS"] if server.offer_starttls and not tls else []) + ["AUTH PLAIN"]
                for i, text in enumerate(lines):
                    reply(("250 " if i == len(lines) - 1 else "250-") + text)
            elif verb == "STARTTLS":
                reply("220 Ready to start TLS")
                sock = server.context.wrap_socket(sock, server_side=True)
                reader, tls = sock.makefile("rb"), True
                self.open += [sock, reader]
                reply = lambda line: sock.sendall(line.encode() + b"\r\n")
            elif verb == "AUTH":
                _, user, password = base64.b64decode(command.split()[2]).decode().split("\0")
                server.events.append(("auth", tls, user, password))
                reply("235 Authenticated" if (user, password) == server.credentials else "535 Bad credentials")
            elif verb == "MAIL":
                reply("250 OK")
            elif verb == "RCPT":
                rcpt = command.split(":", 1)[1].strip().strip("<>")
                reply("550 No such user" if rcpt in server.refuse else "250 OK")
                if rcpt not in server.refuse:
                    server.events.append(("rcpt", tls, rcpt))
            elif verb == "DATA":
                reply("354 End data with <CR><LF>.<CR><LF>")
                data = b""
                while (chunk := reader.readline()) != b".\r\n":
                    data += chunk
                server.events.append(("data", tls, data.decode()))
                reply("250 Queued")
            elif verb == "QUIT":
                reply("221 Bye")
                return
            else:
                reply("502 Not implemented")


class SMTPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, context, implicit_tls=False, offer_starttls=True):
        super().__init__(("127.0.0.1", 0), SMTPHandler)
        self.context, self.implicit_tls, self.offer_starttls = context, implicit_tls, offer_starttls
        self.credentials = ("me@example.com", "s3cret")
        self.refuse = set()
        self.events = []

    def handle_error(self, request, client_address):
        pass


@unittest.skipUnless(shutil.which("openssl"), "openssl required")
class SMTPIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.cert, key = make_cert(cls.root, "server")
        cls.other_ca, _ = make_cert(cls.root, "other")
        cls.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.context.load_cert_chain(cls.cert, key)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def start(self, **kwargs):
        server = SMTPServer(self.context, **kwargs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(timeout=3)))
        return server

    def config(self, server, security="starttls", ca_file=None, password="s3cret"):
        case = Path(tempfile.mkdtemp(dir=self.root))
        secret = case / "secret.txt"
        secret.write_text(password + "\n")
        secret.chmod(0o600)
        (case / "config.yaml").write_text(
            "email:\n  enabled: true\n  smtp_host: localhost\n"
            f"  smtp_port: {server.server_address[1]}\n  security: {security}\n"
            f"  ca_file: {ca_file or self.cert}\n  username: me@example.com\n"
            "  password_file: secret.txt\n  from: ipwatch <ipwatch@example.com>\n"
            "  to: [me@example.com, other@example.com]\n  timeout_seconds: 5\n")
        return load_config(case / "config.yaml").email

    def send_test(self, email):
        return send(email, compose_test(email, "testhost", "example-home"))

    def test_starttls_authenticates_only_after_tls(self):
        server = self.start()
        self.assertEqual(self.send_test(self.config(server)), [])
        auth = [e for e in server.events if e[0] == "auth"]
        self.assertEqual(auth, [("auth", True, "me@example.com", "s3cret")])
        data = [e for e in server.events if e[0] == "data"]
        self.assertTrue(data and data[0][1], "message must be sent inside TLS")
        self.assertIn("Subject: [ipwatch] example-home: test message", data[0][2])
        self.assertEqual({e[2] for e in server.events if e[0] == "rcpt"}, {"me@example.com", "other@example.com"})

    def test_implicit_tls(self):
        server = self.start(implicit_tls=True)
        self.assertEqual(self.send_test(self.config(server, security="tls")), [])
        self.assertTrue(all(e[1] for e in server.events))

    def test_untrusted_certificate_is_rejected_before_credentials(self):
        for kwargs, security in (({}, "starttls"), ({"implicit_tls": True}, "tls")):
            with self.subTest(security):
                server = self.start(**kwargs)
                with self.assertRaisesRegex(NotifyError, "certificate verification failed"):
                    self.send_test(self.config(server, security=security, ca_file=self.other_ca))
                self.assertEqual([e for e in server.events if e[0] in ("auth", "data")], [])

    def test_server_without_starttls_gets_no_credentials(self):
        server = self.start(offer_starttls=False)
        with self.assertRaisesRegex(NotifyError, "does not offer STARTTLS"):
            self.send_test(self.config(server))
        self.assertEqual(server.events, [])

    def test_bad_password(self):
        server = self.start()
        with self.assertRaisesRegex(NotifyError, "authentication failed \\(code 535\\)"):
            self.send_test(self.config(server, password="wrong"))
        self.assertEqual([e for e in server.events if e[0] == "data"], [])

    def test_partially_refused_recipients(self):
        server = self.start()
        server.refuse.add("other@example.com")
        self.assertEqual(self.send_test(self.config(server)), ["other@example.com"])

    def test_cli_test_email_command(self):
        server = self.start()
        email = self.config(server)
        config_path = email.password_file.parent / "config.yaml"
        result = subprocess.run([sys.executable, "-m", "ipwatch", "test-email", "--config", str(config_path)],
            capture_output=True, text=True, timeout=15, env=dict(os.environ))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("accepted by localhost", result.stdout)
        server.refuse.add("other@example.com")
        result = subprocess.run([sys.executable, "-m", "ipwatch", "test-email", "--config", str(config_path)],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 3)
        self.assertIn("recipients refused: other@example.com", result.stderr)


if __name__ == "__main__":
    unittest.main()
