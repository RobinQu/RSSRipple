"""HTTP for externally supplied URLs, with destination checks at connection time.

Private destinations require an administrator-authorized exact origin. Proxies
are deliberately disabled: a proxy could resolve a different destination.
"""

import asyncio
import ipaddress
import socket
import ssl
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore

import httpcore
import httpx

from app.config import settings

Origin = tuple[str, str, int]

# OS DNS calls cannot be forcibly cancelled. Bound both workers and queued
# lookups so timed-out requests cannot accumulate threads or pending work.
_resolver = ThreadPoolExecutor(max_workers=4, thread_name_prefix="outbound-dns")
_resolver_slots = BoundedSemaphore(8)


def _submit_resolution(host: str, port: int):
    if not _resolver_slots.acquire(blocking=False):
        raise httpcore.ConnectError("Outbound DNS resolver is busy")
    try:
        future = _resolver.submit(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    except BaseException:
        _resolver_slots.release()
        raise
    future.add_done_callback(lambda _: _resolver_slots.release())
    return future


def _consume_resolution_error(future):
    # A request may time out or be cancelled before the OS lookup finishes.
    # Retrieving a late error prevents an unobserved asyncio Future warning.
    if not future.cancelled():
        future.exception()


class DestinationDenied(httpx.TransportError):
    """The requested destination is outside the configured outbound policy."""


def origin(url: str | httpx.URL) -> Origin:
    parsed = httpx.URL(url)
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise DestinationDenied("Outbound destination must use HTTP or HTTPS")
    if parsed.userinfo:
        raise DestinationDenied("Credentials in outbound URLs are not permitted")
    return parsed.scheme, parsed.raw_host.decode("ascii"), parsed.port or (
        443 if parsed.scheme == "https" else 80
    )


def _public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if not ip.is_global or ip.is_multicast or ip.is_unspecified:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        # Exclude mapped/translation/tunnel addresses, whose effective IPv4
        # destination cannot be inferred safely from normal IPv6 scope alone.
        return (
            ip in ipaddress.ip_network("2000::/3")
            and ip.sixtofour is None
            and ip.teredo is None
        )
    return True


def _validated_addresses(resolved, *, allow_private: bool) -> list[str]:
    addresses = list(dict.fromkeys(item[4][0] for item in resolved))
    if not addresses:
        raise httpcore.ConnectError("Outbound DNS returned no addresses")
    if not allow_private and not all(_public_address(address) for address in addresses):
        raise DestinationDenied("Outbound destination resolves to a non-public address")
    return addresses


class _CheckedBackend(httpcore.SyncBackend):
    def __init__(self, scheme: str, allowed: frozenset[Origin]):
        self.scheme = scheme
        self.allowed = allowed

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        started = time.monotonic()
        try:
            resolved = _submit_resolution(host, port).result(timeout=timeout)
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout("Outbound DNS resolution timed out") from exc
        except OSError as exc:
            raise httpcore.ConnectError("Outbound DNS resolution failed") from exc
        addresses = _validated_addresses(
            resolved, allow_private=(self.scheme, host, port) in self.allowed,
        )

        # Validate the entire answer before dialing any address. Only numeric
        # addresses reach the underlying backend; Host and TLS SNI stay with
        # the original httpcore connection origin.
        for index, address in enumerate(addresses):
            remaining = None if timeout is None else timeout - (time.monotonic() - started)
            if remaining is not None and remaining <= 0:
                raise httpcore.ConnectTimeout("Outbound connection timed out")
            try:
                return super().connect_tcp(
                    address, port, timeout=remaining, local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout):
                if index == len(addresses) - 1:
                    raise


class _CheckedTransport(httpx.HTTPTransport):
    def __init__(self, scheme: str, allowed: frozenset[Origin], context: ssl.SSLContext, keepalive_connections: int):
        super().__init__(verify=context, trust_env=False)
        self.scheme = scheme
        # HTTPX 0.28's adapter uses _pool for request, stream and exception
        # translation. Keep that adapter, replacing only its unused pool with
        # httpcore's public network_backend extension. Compatibility is tested.
        self._pool.close()
        self._pool = httpcore.ConnectionPool(
            ssl_context=context, network_backend=_CheckedBackend(scheme, allowed),
            max_connections=100, max_keepalive_connections=keepalive_connections,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        scheme, _, _ = origin(request.url)
        if scheme != self.scheme:
            raise DestinationDenied("Outbound transport scheme mismatch")
        return super().handle_request(request)


class _CheckedAsyncBackend(httpcore.AnyIOBackend):
    def __init__(self, scheme: str, allowed: frozenset[Origin]):
        self.scheme = scheme
        self.allowed = allowed

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        started = time.monotonic()
        try:
            pending = asyncio.wrap_future(_submit_resolution(host, port))
            pending.add_done_callback(_consume_resolution_error)
            resolved = await asyncio.wait_for(
                asyncio.shield(pending),
                timeout=timeout,
            )
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout("Outbound DNS resolution timed out") from exc
        except OSError as exc:
            raise httpcore.ConnectError("Outbound DNS resolution failed") from exc
        addresses = _validated_addresses(
            resolved, allow_private=(self.scheme, host, port) in self.allowed,
        )
        for index, address in enumerate(addresses):
            remaining = None if timeout is None else timeout - (time.monotonic() - started)
            if remaining is not None and remaining <= 0:
                raise httpcore.ConnectTimeout("Outbound connection timed out")
            try:
                return await super().connect_tcp(
                    address, port, timeout=remaining, local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout):
                if index == len(addresses) - 1:
                    raise


class _CheckedAsyncTransport(httpx.AsyncHTTPTransport):
    def __init__(self, scheme: str, allowed: frozenset[Origin], context: ssl.SSLContext, keepalive_connections: int):
        # The inherited HTTPX adapter only requires _pool. Construct the
        # controlled pool directly, before any connection can be created.
        self.scheme = scheme
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=context, network_backend=_CheckedAsyncBackend(scheme, allowed),
            max_connections=100, max_keepalive_connections=keepalive_connections,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        scheme, _, _ = origin(request.url)
        if scheme != self.scheme:
            raise DestinationDenied("Outbound transport scheme mismatch")
        return await super().handle_async_request(request)


def client(
    *, timeout: float, headers: dict[str, str] | None = None,
    allowed_origins: Iterable[str] = (), context: ssl.SSLContext | None = None,
    keepalive_connections: int = 20,
) -> httpx.Client:
    """Create a direct client; redirects receive the same destination policy.

    ``allowed_origins`` must come from administrator configuration, never from
    feed entries, metadata results or redirect responses. TLS verification is
    enabled even for an explicitly permitted private origin.
    """
    allowed = frozenset(origin(url) for url in (*settings.outbound_private_origins, *allowed_origins))
    context = context or ssl.create_default_context()
    return httpx.Client(
        timeout=timeout, headers=headers, follow_redirects=True, trust_env=False,
        mounts={scheme + "://": _CheckedTransport(scheme, allowed, context, keepalive_connections)
                for scheme in ("http", "https")},
    )


def async_client(
    *, timeout: float | httpx.Timeout, headers: dict[str, str] | None = None,
    allowed_origins: Iterable[str] = (), context: ssl.SSLContext | None = None,
    keepalive_connections: int = 20,
) -> httpx.AsyncClient:
    """Async equivalent with the same connection and redirect policy."""
    allowed = frozenset(origin(url) for url in (*settings.outbound_private_origins, *allowed_origins))
    context = context or ssl.create_default_context()
    return httpx.AsyncClient(
        timeout=timeout, headers=headers, follow_redirects=True, trust_env=False,
        mounts={scheme + "://": _CheckedAsyncTransport(scheme, allowed, context, keepalive_connections)
                for scheme in ("http", "https")},
    )
