"""Bounded HTTPS requests using the actual curl executable, without a shell."""
import subprocess
import tempfile


class RequestError(RuntimeError):
    pass


class CurlClient:
    def __init__(self, config):
        self.config = config

    def get(self, url, family=None):
        c = self.config
        last_error = None
        for _ in range(c.attempts):
            with tempfile.TemporaryFile() as body:
                # /proc/self/fd resolves in curl; pass_fds keeps this descriptor open.
                cmd = [c.curl_binary, "--disable", "--silent", "--show-error", "--fail",
                    "--proto", "=https", "--connect-timeout", str(c.connect_timeout_seconds),
                    "--max-time", str(c.timeout_seconds), "--max-filesize", "65536",
                    "--output", f"/proc/self/fd/{body.fileno()}", "--write-out", "%{http_code}"]
                if family:
                    cmd.append("--ipv4" if family == "ipv4" else "--ipv6")
                if c.bypass_proxy:
                    cmd.extend(["--noproxy", "*"])
                if c.interface:
                    cmd.extend(["--interface", c.interface])
                cmd.extend(["--url", url])
                try:
                    result = subprocess.run(cmd, capture_output=True, timeout=c.timeout_seconds + 2,
                        check=False, pass_fds=(body.fileno(),))
                except OSError as exc:
                    raise RequestError("Cannot execute configured curl binary") from exc
                except subprocess.TimeoutExpired:
                    last_error = "curl process exceeded timeout"
                    continue
                if result.returncode:
                    # Do not copy remote bodies, URLs, or curl stderr into logs.
                    last_error = f"curl exited with code {result.returncode}"
                    continue
                if not result.stdout.isdigit() or not 200 <= int(result.stdout) <= 299:
                    last_error = "Endpoint did not return a successful HTTP status"
                    continue
                body.seek(0)
                payload = body.read(65537)
                if len(payload) > 65536:
                    raise RequestError("Endpoint response exceeded 64 KiB")
                try:
                    return payload.decode("utf-8")
                except UnicodeError as exc:
                    raise RequestError("Endpoint returned invalid UTF-8") from exc
        raise RequestError(last_error or "Request failed")
