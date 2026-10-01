"""Optional email notification of IP changes, sent only over TLS-protected SMTP."""
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
import os
import smtplib
import ssl
import stat


class NotifyError(RuntimeError):
    pass


def read_password(email):
    """Return the SMTP password, or None when authentication is not configured."""
    if email.username is None:
        return None
    if email.password_env is not None:
        value = os.environ.get(email.password_env)
        if not value:
            raise NotifyError(f"Environment variable {email.password_env} is unset or empty")
        return value
    try:
        fd = os.open(email.password_file, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise NotifyError(f"Cannot open email.password_file: {exc.strerror}") from exc
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        # Like ssh with private keys: refuse secrets that other users could read or replace.
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise NotifyError("email.password_file must be a regular file owned by this user with mode 0600")
        raw = handle.read(4097)
    if len(raw) > 4096:
        raise NotifyError("email.password_file is larger than 4 KiB")
    try:
        password = raw.decode("utf-8").split("\n", 1)[0].rstrip("\r")
    except UnicodeError as exc:
        raise NotifyError("email.password_file is not UTF-8") from exc
    if not password:
        raise NotifyError("email.password_file is empty")
    return password


def tls_context(email):
    # Certificate and hostname verification stay on; only the trusted CA set is configurable.
    try:
        context = ssl.create_default_context(cafile=str(email.ca_file) if email.ca_file else None)
    except (OSError, ssl.SSLError) as exc:
        raise NotifyError(f"Cannot load email.ca_file: {exc}") from exc
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def notice_from(record):
    """The subset of an observation record that an IP-change email needs."""
    return {key: record[key] for key in ("record_id", "timestamp_utc", "hostname", "label",
        "family", "ip", "previous_ip", "ip_source")} | {"geoip": record["geoip"]["data"]}


def clean(value):
    # Provider strings reach the body only; drop control characters all the same.
    return "".join(c for c in str(value) if c.isprintable())


def message(email, subject, body):
    msg = EmailMessage()
    msg["From"] = email.sender
    msg["To"] = ", ".join(email.recipients)
    msg["Subject"] = f"{email.subject_prefix} {subject}".strip()
    msg["Date"] = formatdate(usegmt=True)
    domain = parseaddr(email.sender)[1].rpartition("@")[2]
    msg["Message-ID"] = make_msgid(domain=domain)
    msg.set_content(body)
    return msg


def compose(email, pending):
    n = pending["notice"]
    family = "IPv4" if n["family"] == "ipv4" else "IPv6"
    who = n["label"] or n["hostname"]
    lines = [f"The external {family} address observed by host {clean(n['hostname'])}"
             + (f" (label {clean(n['label'])})" if n["label"] else "") + " changed.", "",
             f"  New IP:       {n['ip']}",
             f"  Previous IP:  {n['previous_ip']}",
             f"  Observed at:  {n['timestamp_utc']}",
             f"  IP source:    {n['ip_source']}"]
    geo = n.get("geoip")
    if geo:
        place = ", ".join(clean(geo[k]) for k in ("city", "region", "country") if geo.get(k))
        if place:
            code = f" ({clean(geo['country_code'])})" if geo.get("country_code") else ""
            lines.append(f"  Location:     {place}{code} (GeoIP estimate)")
        network = " ".join(x for x in (f"AS{geo['asn']}" if geo.get("asn") else "",
                                        clean(geo.get("isp") or "")) if x)
        if network:
            lines.append(f"  Network:      {network}")
    else:
        lines.append("  Location:     unavailable (GeoIP disabled or failed)")
    lines.append(f"  Record ID:    {n['record_id']}")
    if pending.get("attempts"):
        lines += ["", f"Delivery was delayed: {pending['attempts']} earlier attempt(s) failed."
                  f" Last error: {clean(pending.get('last_error'))}"]
    lines += ["", "Sent by ipwatch. The full history is in the JSON Lines log on that host.", ""]
    return message(email, f"{clean(who)}: external {family} changed to {n['ip']}", "\n".join(lines))


def compose_test(email, hostname, label):
    return message(email, f"{clean(label or hostname)}: test message",
        f"This is a test message from ipwatch on host {clean(hostname)}.\n"
        "SMTP connection, TLS verification, authentication and delivery succeeded.\n")


def send(email, msg):
    """Deliver msg; return addresses the server refused while accepting others."""
    password = read_password(email)
    context = tls_context(email)
    try:
        if email.security == "tls":
            client = smtplib.SMTP_SSL(email.host, email.port, timeout=email.timeout_seconds, context=context)
        else:
            client = smtplib.SMTP(email.host, email.port, timeout=email.timeout_seconds)
        try:
            if email.security == "starttls":
                client.ehlo()
                if not client.has_extn("starttls"):
                    raise NotifyError("SMTP server does not offer STARTTLS; nothing was sent")
                client.starttls(context=context)
                client.ehlo()
            if email.username is not None:
                client.login(email.username, password)
            refused = client.send_message(msg)
        finally:
            try:
                client.quit()
            except (smtplib.SMTPException, OSError):
                client.close()
    except ssl.SSLCertVerificationError as exc:
        raise NotifyError(f"SMTP TLS certificate verification failed: {exc.verify_message}") from exc
    except smtplib.SMTPAuthenticationError as exc:
        raise NotifyError(f"SMTP authentication failed (code {exc.smtp_code})") from exc
    except smtplib.SMTPResponseException as exc:
        raise NotifyError(f"SMTP server rejected the message (code {exc.smtp_code})") from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise NotifyError("SMTP server refused all recipients") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise NotifyError(f"SMTP delivery failed: {type(exc).__name__}: {exc}") from exc
    return sorted(refused)
