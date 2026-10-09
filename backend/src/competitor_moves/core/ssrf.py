"""SSRF guard: only public http(s) addresses may be fetched. Every redirect hop is checked by the caller.

ponytail: the host is resolved here and again by the HTTP client, so a DNS-rebinding attacker could swap the
address in between. Pin the resolved IP in the client if that threat matters.
"""
import ipaddress
import socket
from urllib.parse import urlsplit

# "host:port" pairs that may be reached even though they are local. Tests add their own mock server here;
# nothing in the application adds to it.
ALLOW_LOCAL: set[str] = set()


class BlockedURL(ValueError):
    pass


def is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%", 1)[0])  # drop IPv6 zone id
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


def check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedURL("only http(s) URLs are allowed")
    if not parts.hostname:
        raise BlockedURL("URL has no host")
    if parts.username or parts.password:
        raise BlockedURL("URLs with credentials are not allowed")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise BlockedURL("invalid port") from None
    if f"{parts.hostname}:{port}" in ALLOW_LOCAL:
        return
    try:
        infos = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise BlockedURL(f"cannot resolve {parts.hostname}") from None
    if not infos or not all(is_public_ip(info[4][0]) for info in infos):
        raise BlockedURL("private/internal addresses are blocked")
