"""HTTP/HTTPS backend (read-only).

Write and listing operations are infeasible over plain HTTP and raise
`UnsupportedOperationError`. `read_range` verifies the server honored the `Range`
header (206) and refuses to pass off a full-body 200 as a range.
"""

from __future__ import annotations

import contextlib
import os
import threading

from ondio.backends.protocol import _replace_on_success
from ondio.types import (
    AuthError,
    NetworkError,
    ObjectNotFoundError,
    OndioError,
    UnsupportedOperationError,
)

_CHUNK = 1024 * 1024


def _session_class(requests, header_names: tuple[str, ...]):
    """A `requests.Session` that keeps the caller's cookies and headers off other hosts."""

    class Session(requests.Session):
        def rebuild_auth(self, prepared_request, response):
            # requests drops only Authorization when a redirect goes to another host.
            if self.should_strip_auth(response.request.url, prepared_request.url):
                prepared_request.headers.pop("Cookie", None)
                for name in header_names:
                    prepared_request.headers.pop(name, None)
            super().rebuild_auth(prepared_request, response)

    return Session


class HttpBackend:
    """Read-only `StorageBackend` for plain `http(s)://` URLs, via `requests`.

    See [`StorageBackend`][ondio.backends.StorageBackend] for method
    semantics. Requires the `http` extra. `upload`, `list_files`,
    `object_count`, and `delete` raise `UnsupportedOperationError` —
    infeasible over plain HTTP. `read_range` verifies the server honored the
    `Range` header (206) and refuses to pass off a full-body 200 as a range.

    `requests` errors are translated: a bad URL or header is `ValueError`, a connection
    failure or timeout is `NetworkError`, and any other is `OndioError`.

    For a bearer token, give `headers={"Authorization": "Bearer <token>"}`.
    A redirect to another host gets no `Authorization`, no cookies, and none of
    the `headers` given here.
    No error message holds an option's value.

    Args:
        headers: Headers sent with every request. A `Range` header is refused:
            `read` would send it and get part of the file.
        auth: Anything `requests` takes as `auth`, e.g. a `(user, password)` pair.
        cookies: Cookies sent with every request.
        verify: `True` checks the server's TLS certificate, `False` does not,
            and a path names the CA bundle to check it with.
        timeout: Seconds to wait, as `requests` takes it: one number, or a
            `(connect, read)` pair. The read timeout limits each wait for data,
            not the whole request, so a server that keeps sending slowly is not
            stopped by it. `None` waits with no limit.
        allow_redirects: Follow redirects. If `False`, a 3xx answer is an
            `OndioError`.

    Raises:
        ValueError: If `headers` has a `Range` key, in any case.
    """

    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        auth=None,
        cookies: dict[str, str] | None = None,
        verify: bool | str = True,
        timeout: float | tuple[float, float] | None = (10, 60),
        allow_redirects: bool = True,
    ):
        try:
            import requests
        except ImportError as exc:
            raise ImportError(
                "http(s):// support requires the 'http' extra: pip install ondio[http]"
            ) from exc
        if any(name.lower() == "range" for name in headers or {}):
            raise ValueError("headers must not hold a Range header; use read_range")
        self._requests = requests
        # Copies, so a later change to the caller's dicts does nothing.
        self._headers = dict(headers or {})
        self._session_class = _session_class(requests, tuple(self._headers))
        self._cookies = dict(cookies or {})
        self._auth = auth
        self._verify = verify
        self._timeout = timeout
        self._allow_redirects = allow_redirects
        self._local = threading.local()  # requests.Session is not thread-safe

    @property
    def _session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._local.session = self._session_class()
            session.headers.update(self._headers)
            session.cookies.update(self._cookies)
            session.auth = self._auth
        return session

    @contextlib.contextmanager
    def _translate(self, uri: str):
        exceptions = self._requests.exceptions
        try:
            yield
        except ValueError as exc:
            # requests raises ValueErrors (InvalidURL, InvalidHeader, ...) for a bad
            # request, not a failed fetch. Their text can hold a header value.
            raise ValueError(f"invalid HTTP request for {uri}: {type(exc).__name__}") from exc
        # ChunkedEncodingError: the body stopped in the middle.
        except (
            exceptions.ConnectionError,
            exceptions.Timeout,
            exceptions.ChunkedEncodingError,
        ) as exc:
            raise NetworkError(f"HTTP request failed for {uri}: {type(exc).__name__}") from exc
        except exceptions.RequestException as exc:
            raise OndioError(f"HTTP request failed for {uri}: {type(exc).__name__}") from exc

    # verify is passed per request, not set on the session: requests replaces
    # session.verify with REQUESTS_CA_BUNDLE or CURL_CA_BUNDLE when either is set.
    def _get(self, uri: str, **kwargs):
        return self._session.get(
            uri,
            timeout=self._timeout,
            allow_redirects=self._allow_redirects,
            verify=self._verify,
            **kwargs,
        )

    def _head(self, uri: str):
        return self._session.head(
            uri, timeout=self._timeout, allow_redirects=self._allow_redirects, verify=self._verify
        )

    def _check_status(self, uri: str, response) -> None:
        if response.status_code == 404:
            raise ObjectNotFoundError(f"no such object: {uri}")
        if response.status_code in (401, 403):
            raise AuthError(f"access denied ({response.status_code}): {uri}")
        if response.status_code >= 400:
            raise OndioError(f"HTTP {response.status_code} for {uri}")
        if response.status_code >= 300:
            # Without this, read would return the redirect page as the file.
            raise OndioError(f"HTTP {response.status_code} for {uri}: redirect not followed")

    def read(self, uri: str) -> bytes:
        with self._translate(uri):
            response = self._get(uri)
        self._check_status(uri, response)
        return response.content

    def read_range(self, uri: str, start_byte: int, end_byte: int | None) -> bytes:
        if start_byte < 0:
            raise ValueError(f"start_byte must be >= 0, got {start_byte}")
        if end_byte is not None and end_byte < start_byte:
            raise ValueError(f"end_byte {end_byte} < start_byte {start_byte}")
        header = f"bytes={start_byte}-" if end_byte is None else f"bytes={start_byte}-{end_byte}"
        with self._translate(uri):
            response = self._get(uri, headers={"Range": header})
        if response.status_code == 416:
            return b""  # start past EOF: protocol says return the available bytes
        if response.status_code == 200:
            raise UnsupportedOperationError(
                f"server ignored the Range header (answered 200, not 206): {uri}"
            )
        self._check_status(uri, response)
        if response.status_code != 206:
            raise OndioError(f"unexpected status {response.status_code} for ranged GET: {uri}")
        return response.content

    def size(self, uri: str) -> int:
        with self._translate(uri):
            response = self._head(uri)
        self._check_status(uri, response)
        length = response.headers.get("Content-Length")
        if length is None:
            raise UnsupportedOperationError(f"server reports no Content-Length: {uri}")
        return int(length)

    def download(self, uri: str, out_path: str | os.PathLike[str]) -> None:
        with (
            _replace_on_success(out_path) as temporary,
            self._translate(uri),
            self._get(uri, stream=True) as response,
        ):
            self._check_status(uri, response)
            with open(temporary, "wb") as f:
                for chunk in response.iter_content(_CHUNK):
                    f.write(chunk)

    def exists(self, uri: str) -> bool:
        with self._translate(uri):
            response = self._head(uri)
        if response.status_code in (405, 501):  # server does not support HEAD
            with self._translate(uri), self._get(uri, stream=True) as get_response:
                if get_response.status_code == 404:
                    return False
                self._check_status(uri, get_response)
                return True
        if response.status_code == 404:
            return False
        self._check_status(uri, response)
        return True

    # --- infeasible over plain HTTP -------------------------------------

    def write(self, uri: str, data: bytes) -> None:
        raise UnsupportedOperationError(f"write is not supported over HTTP: {uri}")

    def create(self, uri: str, data: bytes) -> None:
        raise UnsupportedOperationError(f"create is not supported over HTTP: {uri}")

    def upload(self, uri: str, source_path: str | os.PathLike[str]) -> None:
        raise UnsupportedOperationError(f"upload is not supported over HTTP: {uri}")

    def list_files(
        self,
        uri_prefix: str,
        *,
        recursive: bool = True,
        max_items: int | None = None,
        required_prefix: str | None = None,
        required_ext: str | None = None,
    ) -> list[str]:
        raise UnsupportedOperationError(f"list_files is not supported over HTTP: {uri_prefix}")

    def object_count(self, uri_prefix: str, *, required_prefix: str | None = None, required_ext: str | None = None) -> int:
        raise UnsupportedOperationError(f"object_count is not supported over HTTP: {uri_prefix}")

    def delete(self, uri: str) -> None:
        raise UnsupportedOperationError(f"delete is not supported over HTTP: {uri}")
