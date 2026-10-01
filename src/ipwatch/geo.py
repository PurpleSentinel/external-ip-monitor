"""Provider adapter for the ipwho.is JSON response schema."""
import ipaddress
import json
from .transport import RequestError


class GeoError(ValueError):
    pass


def text(value):
    return value if isinstance(value, str) else None


def number(value):
    return value if type(value) in (int, float) and -180 <= value <= 180 else None


def lookup(client, endpoint, ip):
    try:
        data = json.loads(client.get(endpoint.replace("{ip}", ip)))
    except (ValueError, RequestError) as exc:
        raise GeoError(str(exc) if isinstance(exc, RequestError) else "GeoIP returned invalid JSON") from exc
    if not isinstance(data, dict) or data.get("success") is not True:
        raise GeoError("GeoIP provider rejected the lookup")
    try:
        if ipaddress.ip_address(data.get("ip", "")) != ipaddress.ip_address(ip):
            raise GeoError("GeoIP response IP does not match requested IP")
    except ValueError as exc:
        raise GeoError("GeoIP response has an invalid or mismatched IP") from exc
    connection = data.get("connection")
    connection = connection if isinstance(connection, dict) else {}
    timezone = data.get("timezone")
    timezone = timezone if isinstance(timezone, dict) else {}
    latitude = number(data.get("latitude"))
    return {"continent": text(data.get("continent")), "country": text(data.get("country")),
        "country_code": text(data.get("country_code")), "region": text(data.get("region")),
        "city": text(data.get("city")), "latitude": latitude if latitude is None or abs(latitude) <= 90 else None,
        "longitude": number(data.get("longitude")), "timezone": text(timezone.get("id")),
        "asn": connection.get("asn") if type(connection.get("asn")) is int else None,
        "isp": text(connection.get("isp")), "organization": text(connection.get("org"))}
