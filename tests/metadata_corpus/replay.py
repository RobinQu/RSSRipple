"""HTTP transport cassettes: exact requests, no fallback to the network."""

from __future__ import annotations

import base64
import json
import socket
import threading
from collections import Counter
from contextlib import ExitStack
from contextvars import ContextVar
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .dataset import digest, read_json, write_json

SECRET_KEYS = {"api_key", "apikey", "token", "access_token", "key", "passkey", "password", "authorization"}
SEMANTIC_HEADERS = {"accept-language", "openai-beta", "anthropic-version", "anthropic-beta", "x-api-version"}
_PERMITTED_TRANSPORT = ContextVar("corpus_permitted_transport", default=False)


class ReplayError(AssertionError):
    """Recorded evidence is incomplete or incompatible, never a source miss."""


def clean_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    query = [(key, "REDACTED" if key.lower() in SECRET_KEYS else value) for key, value in parse_qsl(parts.query)]
    return urlunsplit((parts.scheme, host, parts.path, urlencode(sorted(query)), ""))


def redact(value):
    if isinstance(value, dict):
        return {key: ("REDACTED" if key.lower() in SECRET_KEYS else redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and value.startswith(("https://", "http://")):
        return clean_url(value)
    return value


def request_key(request: httpx.Request) -> tuple[str, dict]:
    raw = request.content
    content_type = request.headers.get("content-type", "")
    if "json" in content_type and raw:
        body = json.loads(raw)
        # Full messages/tool schemas/model settings remain in the fingerprint.
        # Neither API credentials nor provider-specific auth headers are stored.
        body = redact(body)
        raw = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    elif raw:
        raise ReplayError("non-JSON request body requires an explicit credential-safe adapter")
    safe = {"method": request.method, "url": clean_url(str(request.url)),
            "body": base64.b64encode(raw).decode(), "content_type": content_type}
    headers = {key: value for key, value in request.headers.items() if key in SEMANTIC_HEADERS}
    if headers:
        safe["semantic_headers"] = headers
    return digest(json.dumps(safe, sort_keys=True).encode()), safe


class Cassette:
    """Patch the actual HTTP transport; production parsers and judges still run.

    `record` must be explicit, targets a new file, and only permits listed
    provider hosts. `llm` replays sources but permits the configured LLM host.
    All swallowed transport failures remain in errors for final verification.
    """

    def __init__(self, path: Path, *, mode="replay", source_hosts=(), llm_host="", local_assets=None,
                 database_address=None, seed=None):
        if mode not in {"replay", "record", "record-llm", "llm"}:
            raise ValueError("invalid cassette mode")
        if mode in {"record", "record-llm"} and path.exists():
            raise ValueError("refusing to overwrite recorded evidence")
        self.path, self.mode = path, mode
        self.source_hosts, self.llm_host = set(source_hosts), llm_host
        self.assets = local_assets or {}
        self.data = read_json(seed or path) if seed or path.exists() else {"version": 1, "requests": {}}
        self.errors: list[str] = []
        self.calls = Counter()
        self.consumed = Counter()
        self.database_address = database_address
        self.lock = threading.Lock()
        self.stack = ExitStack()

    def _lookup(self, request):
        try:
            key, safe = request_key(request)
        except ReplayError as error:
            return self._fail(str(error))
        self.calls[request.url.host] += 1
        if str(request.url) in self.assets:
            return httpx.Response(200, content=self.assets[str(request.url)], request=request)
        if self.mode == "record":
            if request.url.host in self.source_hosts or request.url.host == self.llm_host:
                return None
            return self._fail(f"recording host not allowed: {request.url.host}")
        if self.mode in {"llm", "record-llm"} and request.url.host == self.llm_host:
            return None
        row = self.data["requests"].get(key)
        if row is None:
            return self._fail(f"unrecorded request: {safe['method']} {safe['url']} fingerprint={key}")
        self.consumed[key] += 1
        if self.consumed[key] > row.get("count", 1):
            return self._fail(f"recorded request overused: {key}")
        if "error" in row:
            error_type = getattr(httpx, row["error"], httpx.TransportError)
            if not isinstance(error_type, type) or not issubclass(error_type, httpx.TransportError):
                error_type = httpx.TransportError
            raise error_type(f"recorded transport failure: {row['error']}", request=request)
        return httpx.Response(row["status"], headers=row["headers"],
                              content=base64.b64decode(row["body"]), request=request)

    def _fail(self, message):
        self.errors.append(message)
        raise ReplayError(message)

    def _record(self, request, response):
        if self.mode in {"llm", "record-llm"} and request.url.host == self.llm_host and response.is_error:
            self.errors.append(f"live LLM HTTP failure: {response.status_code}")
        if self.mode not in {"record", "record-llm"}:
            return
        key, safe = request_key(request)
        body = response.content
        if "json" in response.headers.get("content-type", ""):
            body = json.dumps(redact(response.json()), ensure_ascii=False).encode()
        row = {"request": safe, "status": response.status_code,
               "headers": {k: clean_url(v) if k == "location" else v for k, v in response.headers.items()
                           if k in {"content-type", "location"}},
               "body": base64.b64encode(body).decode()}
        with self.lock:
            previous = self.data["requests"].get(key)
            if previous and {k: v for k, v in previous.items() if k != "count"} != row:
                self._fail(f"non-repeatable response for request {key}; split this scenario into phases")
            row["count"] = previous.get("count", 1) + 1 if previous else 1
            self.data["requests"][key] = row

    def _transport_error(self, request, error):
        if self.mode in {"record", "record-llm"}:
            key, safe = request_key(request)
            previous = self.data["requests"].get(key)
            if previous and previous.get("error") != type(error).__name__:
                self._fail(f"non-repeatable transport outcome for request {key}")
            self.data["requests"][key] = {"request": safe, "error": type(error).__name__,
                                          "count": previous.get("count", 1) + 1 if previous else 1}
        elif self.mode == "llm":
            self.errors.append(f"live LLM transport failure: {type(error).__name__}")

    def __enter__(self):
        original_sync = httpx.HTTPTransport.handle_request
        original_async = httpx.AsyncHTTPTransport.handle_async_request

        def sync_send(transport, request):
            response = self._lookup(request)
            if response is not None:
                return response
            try:
                token = _PERMITTED_TRANSPORT.set(True)
                response = original_sync(transport, request)
            except httpx.TransportError as error:
                self._transport_error(request, error)
                raise
            finally:
                _PERMITTED_TRANSPORT.reset(token)
            response.read()
            self._record(request, response)
            return response

        async def async_send(transport, request):
            await request.aread()
            response = self._lookup(request)
            if response is not None:
                # Async clients require an AsyncByteStream at this boundary.
                content = response.content
                class Stream(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        yield content
                response.stream = Stream()
                return response
            try:
                token = _PERMITTED_TRANSPORT.set(True)
                response = await original_async(transport, request)
            except httpx.TransportError as error:
                self._transport_error(request, error)
                raise
            finally:
                _PERMITTED_TRANSPORT.reset(token)
            await response.aread()
            self._record(request, response)
            return response

        self.stack.enter_context(patch.object(httpx.HTTPTransport, "handle_request", sync_send))
        self.stack.enter_context(patch.object(httpx.AsyncHTTPTransport, "handle_async_request", async_send))
        # Catch urllib/feedparser/other accidental sockets that bypass httpx.
        # Database access is a separate local socket and remains available.
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex
        def connect(sock, address):
            if not _PERMITTED_TRANSPORT.get() and address != self.database_address:
                self._fail("unrecorded non-HTTP network connection")
            return original_connect(sock, address)
        self.stack.enter_context(patch.object(socket.socket, "connect", connect))
        def connect_ex(sock, address):
            if not _PERMITTED_TRANSPORT.get() and address != self.database_address:
                self._fail("unrecorded non-HTTP network connection")
            return original_connect_ex(sock, address)
        self.stack.enter_context(patch.object(socket.socket, "connect_ex", connect_ex))
        return self

    def __exit__(self, *exc):
        self.stack.close()
        if self.mode in {"record", "record-llm"} and exc[0] is None and not self.errors:
            write_json(self.path, self.data)

    def assert_complete(self):
        if self.mode != "record":
            for key, row in self.data["requests"].items():
                if self.mode in {"llm", "record-llm"} and urlsplit(row["request"]["url"]).hostname == self.llm_host:
                    continue
                if self.consumed[key] != row.get("count", 1):
                    self.errors.append(f"recorded request not fully consumed: {key}")
        if self.mode == "llm" and not self.calls[self.llm_host]:
            self.errors.append("LLM quality run did not invoke the real model")
        if self.errors:
            raise ReplayError("\n".join(sorted(set(self.errors))))
