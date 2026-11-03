"""Where the crawler may connect: public IP addresses only (the SSRF defence).

Checking a URL once is not enough. DNS can answer "public" when the URL is
validated and "169.254.169.254" a moment later, when the request connects
(DNS rebinding). So the check lives where the socket is opened:

    GuardedTransport -> httpcore ConnectionPool -> GuardedNetworkBackend.connect_tcp
        1. resolve the host name ourselves (getaddrinfo, with a timeout);
        2. refuse if *any* resolved address is not public;
        3. connect to one of exactly those validated IP addresses.

There is no second DNS lookup between the check and the connection, so there
is nothing to rebind. The URL keeps the host name, so the Host header, TLS SNI,
and certificate verification still use the name, not the IP. Proxies from the
environment are ignored (`trust_env=False`): a proxy would make the connection
somewhere we never checked. Unix sockets are refused.

"Public" means `is_global` and none of loopback, private, link-local,
multicast, reserved, unspecified, or site-local. IPv6 forms that embed an
IPv4 address (IPv4-mapped `::ffff:a.b.c.d`, 6to4, Teredo, NAT64
`64:ff9b::/96`) are judged by the embedded address too.
"""

import concurrent.futures
import ipaddress
import logging
import socket
import ssl
from collections.abc import Callable, Iterable

import httpcore
import httpx

from app.crawler.urls import BLOCKED_HOST_NAMES, BLOCKED_HOST_SUFFIXES, ip_literal

logger = logging.getLogger(__name__)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], list[str]]
AddressPolicy = Callable[[IPAddress], bool]

DNS_TIMEOUT_SECONDS = 5.0
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_dns_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="dns")


class BlockedDestination(Exception):
    """The destination is not a public address. `message` is safe to show the user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class DnsFailure(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def is_public_address(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.is_site_local:
            return False
        embedded = _embedded_ipv4(ip)
        if embedded is not None and not is_public_address(embedded):
            return False
    return ip.is_global and not (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def check_address(ip: IPAddress, policy: AddressPolicy = is_public_address) -> None:
    if not policy(ip):
        raise BlockedDestination(
            "The address is not a public internet address (loopback, private, link-local, "
            "multicast, and reserved ranges can't be crawled)."
        )


def check_host_name(host: str, policy: AddressPolicy = is_public_address) -> None:
    """Reject host names that can only mean this machine or a private network.

    `host` is already normalised (app.crawler.urls.normalize_host).
    """
    literal = ip_literal(host)
    if literal is not None:
        check_address(literal, policy)
        return
    if host in BLOCKED_HOST_NAMES or host.endswith(BLOCKED_HOST_SUFFIXES):
        raise BlockedDestination("Local and internal host names can't be crawled.")
    if "." not in host:
        # Single-label names resolve through search domains or container DNS
        # (e.g. Docker service names such as "db" or "api").
        raise BlockedDestination("The host name must be a public domain name (like example.com).")


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for _family, _type, _proto, _name, sockaddr in infos:
        address = str(sockaddr[0]).split("%", 1)[0]  # drop an IPv6 scope id
        if address not in addresses:
            addresses.append(address)
    return addresses


def resolve_public(
    host: str,
    port: int,
    *,
    resolver: Resolver = system_resolver,
    policy: AddressPolicy = is_public_address,
    timeout: float = DNS_TIMEOUT_SECONDS,
) -> list[str]:
    """Every address `host` resolves to, if all of them are public; else raise.

    All addresses must pass, not just one: a client could otherwise be steered
    to the private one.
    """
    check_host_name(host, policy)
    literal = ip_literal(host)
    if literal is not None:
        return [str(literal)]
    future = _dns_pool.submit(resolver, host, port)
    try:
        addresses = future.result(timeout=timeout)
    except concurrent.futures.TimeoutError as exc:
        raise DnsFailure(f"Looking up {host} timed out.") from exc
    except (OSError, UnicodeError) as exc:
        raise DnsFailure(f"The host name {host} could not be resolved.") from exc
    if not addresses:
        raise DnsFailure(f"The host name {host} could not be resolved.")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise BlockedDestination("The host name resolved to an invalid address.") from exc
        if not policy(ip):
            logger.warning("Blocked crawl destination host=%s address=%s", host, address)
            raise BlockedDestination(
                f"{host} resolves to a non-public address, so it can't be crawled."
            )
    return addresses


class GuardedNetworkBackend(httpcore.SyncBackend):
    """Opens TCP connections only to addresses that were validated just now."""

    def __init__(self, *, resolver: Resolver, policy: AddressPolicy, dns_timeout: float) -> None:
        self._resolver = resolver
        self._policy = policy
        self._dns_timeout = dns_timeout

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        addresses = resolve_public(
            host, port, resolver=self._resolver, policy=self._policy, timeout=self._dns_timeout
        )
        failure: Exception | None = None
        for address in addresses:
            try:
                # An IP literal: create_connection does no DNS lookup of its own.
                return super().connect_tcp(address, port, timeout, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                failure = exc
        assert failure is not None
        raise failure

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        raise BlockedDestination("Unix sockets can't be crawled.")


class GuardedTransport(httpx.HTTPTransport):
    """httpx's own transport, with its connection pool on the guarded backend."""

    def __init__(
        self,
        *,
        resolver: Resolver = system_resolver,
        policy: AddressPolicy = is_public_address,
        dns_timeout: float = DNS_TIMEOUT_SECONDS,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        super().__init__(trust_env=False, retries=0)
        self._pool.close()
        self.network_backend = GuardedNetworkBackend(
            resolver=resolver, policy=policy, dns_timeout=dns_timeout
        )
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl_context or httpx.create_ssl_context(trust_env=False),
            max_connections=4,
            max_keepalive_connections=2,
            keepalive_expiry=5.0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=self.network_backend,
        )


def guarded_client(
    *,
    timeout: float,
    resolver: Resolver = system_resolver,
    policy: AddressPolicy = is_public_address,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """The only HTTP client the crawler uses. Redirects are handled by the caller."""
    return httpx.Client(
        transport=transport or GuardedTransport(resolver=resolver, policy=policy),
        timeout=httpx.Timeout(timeout),
        follow_redirects=False,
        trust_env=False,
    )
