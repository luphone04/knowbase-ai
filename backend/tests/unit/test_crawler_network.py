"""SSRF defence: the address policy, DNS checks, and the guarded connection itself.

The end-to-end tests run a real HTTP server on 127.0.0.1 and prove the guarded
client never connects to it, whatever name or redirect points there, and that
the transport does connect to the exact address it validated when the policy
allows it.
"""

import http.server
import ipaddress
import threading
import time
from collections.abc import Iterator

import httpcore
import pytest

from app.crawler.fetcher import FetchError, SafeFetcher
from app.crawler.network import (
    BlockedDestination,
    DnsFailure,
    GuardedNetworkBackend,
    check_host_name,
    guarded_client,
    is_public_address,
    resolve_public,
)
from app.crawler.urls import normalize_url, origin_of


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # loopback
        "127.255.255.254",
        "10.0.0.5",  # private
        "172.16.0.1",
        "172.18.0.2",  # a typical Docker bridge address
        "192.168.1.1",
        "169.254.169.254",  # link-local: cloud metadata
        "100.100.100.200",  # carrier-grade NAT: Alibaba metadata
        "0.0.0.0",
        "224.0.0.1",  # multicast
        "239.255.255.250",
        "240.0.0.1",  # reserved
        "255.255.255.255",
        "192.0.2.10",  # documentation
        "198.18.0.1",  # benchmarking
        "::1",  # IPv6 loopback
        "::",
        "fe80::1",  # IPv6 link-local
        "fc00::1",  # unique local
        "fd00:ec2::254",  # AWS IPv6 metadata
        "fec0::1",  # deprecated site-local
        "ff02::1",  # IPv6 multicast
        "2001:db8::1",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:10.0.0.1",
        "2002:7f00:1::",  # 6to4 wrapping 127.0.0.1
        "2002:a00:1::",  # 6to4 wrapping 10.0.0.1
        "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
        "::127.0.0.1",  # IPv4-compatible (deprecated)
    ],
)
def test_non_public_addresses_are_refused(address: str) -> None:
    assert not is_public_address(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert is_public_address(ipaddress.ip_address(address))


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "app.localhost",
        "printer.local",
        "metadata.google.internal",
        "host.docker.internal",
        "router.home.arpa",
        "db",  # single label: Docker service names
        "api",
        "127.0.0.1",
        "10.1.2.3",
        "169.254.169.254",
        "::1",
        "::ffff:127.0.0.1",
    ],
)
def test_internal_host_names_and_literals_are_refused(host: str) -> None:
    with pytest.raises(BlockedDestination):
        check_host_name(host)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.1/",
        "http://0x7f000001/",
        "http://2130706433/",
        "http://017700000001/",  # octal
        "http://[::1]/",
        "http://[::ffff:7f00:1]/",
        "http://LOCALHOST:8000/",
    ],
)
def test_obfuscated_loopback_urls_are_refused(url: str) -> None:
    host = origin_of(normalize_url(url)).host
    with pytest.raises(BlockedDestination):
        check_host_name(host)


def test_dns_answers_must_all_be_public() -> None:
    assert resolve_public("shop.example.com", 443, resolver=lambda h, p: ["93.184.216.34"]) == [
        "93.184.216.34"
    ]
    with pytest.raises(BlockedDestination, match="non-public"):
        resolve_public("evil.example.com", 443, resolver=lambda h, p: ["10.0.0.7"])
    # One private answer among public ones is enough to refuse: the client could pick it.
    with pytest.raises(BlockedDestination):
        resolve_public(
            "mixed.example.com", 443, resolver=lambda h, p: ["93.184.216.34", "127.0.0.1"]
        )
    with pytest.raises(BlockedDestination):
        resolve_public("v6.example.com", 443, resolver=lambda h, p: ["::ffff:169.254.169.254"])


def test_dns_failures_are_reported() -> None:
    def nxdomain(_host: str, _port: int) -> list[str]:
        raise OSError("Name or service not known")

    with pytest.raises(DnsFailure, match="could not be resolved"):
        resolve_public("missing.example.com", 443, resolver=nxdomain)
    with pytest.raises(DnsFailure):
        resolve_public("empty.example.com", 443, resolver=lambda h, p: [])

    def slow(_host: str, _port: int) -> list[str]:
        time.sleep(0.5)
        return ["93.184.216.34"]

    with pytest.raises(DnsFailure, match="timed out"):
        resolve_public("slow.example.com", 443, resolver=slow, timeout=0.05)


# --- the guarded connection ------------------------------------------------------------


@pytest.fixture
def connections(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Records (host, port) the guarded backend actually asks the OS to connect to."""
    calls: list[tuple[str, int]] = []

    def fake_connect(
        self: httpcore.SyncBackend, host: str, port: int, *args: object, **kwargs: object
    ) -> httpcore.NetworkStream:
        calls.append((host, port))
        raise httpcore.ConnectError("test: not really connecting")

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", fake_connect)
    return calls


def test_backend_connects_only_to_the_validated_ip(connections: list[tuple[str, int]]) -> None:
    backend = GuardedNetworkBackend(
        resolver=lambda h, p: ["93.184.216.34"], policy=is_public_address, dns_timeout=1
    )
    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("shop.example.com", 443)
    # The OS is given the IP literal, never the name, so it does no DNS lookup of its own.
    assert connections == [("93.184.216.34", 443)]


def test_backend_refuses_before_any_connection(connections: list[tuple[str, int]]) -> None:
    backend = GuardedNetworkBackend(
        resolver=lambda h, p: ["169.254.169.254"], policy=is_public_address, dns_timeout=1
    )
    with pytest.raises(BlockedDestination):
        backend.connect_tcp("metadata.example.com", 80)
    with pytest.raises(BlockedDestination):
        backend.connect_unix_socket("/var/run/docker.sock")
    assert connections == []


def test_dns_rebinding_is_caught_at_connection_time(connections: list[tuple[str, int]]) -> None:
    """Public when first checked, private when the next connection opens: refused."""
    answers = iter([["93.184.216.34"], ["127.0.0.1"]])
    backend = GuardedNetworkBackend(
        resolver=lambda h, p: next(answers), policy=is_public_address, dns_timeout=1
    )
    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("rebind.example.com", 80)  # public: allowed (fake connect fails)
    with pytest.raises(BlockedDestination):
        backend.connect_tcp("rebind.example.com", 80)  # now private: no connection attempted
    assert connections == [("93.184.216.34", 80)]


# --- end to end against a real local server ---------------------------------------------


class _Recorder(http.server.BaseHTTPRequestHandler):
    hits: list[tuple[str, str | None]] = []

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        type(self).hits.append((self.path, self.headers.get("Host")))
        if self.path == "/redirect-to-metadata":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
            return
        body = b"<html><title>internal</title><body>secret internal page</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def local_server() -> Iterator[int]:
    _Recorder.hits = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _fetcher(url: str, *, resolver: object, policy: object = is_public_address) -> SafeFetcher:
    return SafeFetcher(
        origin=origin_of(url),
        client=guarded_client(timeout=5, resolver=resolver, policy=policy),  # type: ignore[arg-type]
        user_agent="KnowbaseBot/0.1 (+test)",
        timeout=5,
        resolver=resolver,  # type: ignore[arg-type]
        policy=policy,  # type: ignore[arg-type]
    )


def test_real_client_never_reaches_a_loopback_server(local_server: int) -> None:
    for url in (f"http://127.0.0.1:{local_server}/", f"http://localhost:{local_server}/"):
        fetcher = _fetcher(normalize_url(url), resolver=lambda h, p: ["127.0.0.1"])
        with pytest.raises(FetchError) as excinfo:
            fetcher.get(normalize_url(url), accept=None, max_bytes=1024)
        assert excinfo.value.reason == "blocked_destination"
    # A public-looking name that resolves to loopback is refused too.
    url = normalize_url(f"http://innocent.example.com:{local_server}/")
    fetcher = _fetcher(url, resolver=lambda h, p: ["127.0.0.1"])
    with pytest.raises(FetchError, match="non-public"):
        fetcher.get(url, accept=None, max_bytes=1024)
    assert _Recorder.hits == []


def test_connection_level_check_holds_even_without_the_preflight(local_server: int) -> None:
    """Bypass the fetcher entirely: the raw guarded client still refuses to connect."""
    client = guarded_client(timeout=5, resolver=lambda h, p: ["127.0.0.1"])
    with pytest.raises(BlockedDestination):
        client.get(f"http://innocent.example.com:{local_server}/")
    with pytest.raises(BlockedDestination):
        client.get(f"http://127.0.0.1:{local_server}/")
    assert _Recorder.hits == []


def test_environment_proxies_are_ignored(
    local_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A proxy would make the connection somewhere we never validated.
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{local_server}")
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{local_server}")
    monkeypatch.setenv("ALL_PROXY", f"http://127.0.0.1:{local_server}")
    client = guarded_client(timeout=2, resolver=lambda h, p: ["10.9.9.9"])
    with pytest.raises(BlockedDestination):
        client.get("http://shop.example.com/")
    assert _Recorder.hits == []


def test_transport_pins_the_validated_ip_and_keeps_the_host_name(local_server: int) -> None:
    """With a test policy that trusts this one loopback server, the request goes
    through: to the resolved IP, with the original Host header."""
    loopback = ipaddress.ip_address("127.0.0.1")
    url = normalize_url(f"http://site.example.com:{local_server}/page")
    fetcher = _fetcher(url, resolver=lambda h, p: ["127.0.0.1"], policy=lambda ip: ip == loopback)
    response = fetcher.get(url, accept=("text/html",), max_bytes=1024)
    assert response.status == 200
    assert b"secret internal page" in response.body
    assert _Recorder.hits == [("/page", f"site.example.com:{local_server}")]


def test_redirect_to_a_metadata_address_is_never_followed(local_server: int) -> None:
    loopback = ipaddress.ip_address("127.0.0.1")
    url = normalize_url(f"http://site.example.com:{local_server}/redirect-to-metadata")
    fetcher = _fetcher(url, resolver=lambda h, p: ["127.0.0.1"], policy=lambda ip: ip == loopback)
    with pytest.raises(FetchError) as excinfo:
        fetcher.get(url, accept=("text/html",), max_bytes=1024)
    assert excinfo.value.reason == "cross_origin_redirect"
    assert _Recorder.hits == [("/redirect-to-metadata", f"site.example.com:{local_server}")]
