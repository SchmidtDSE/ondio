"""HTTP/HTTPS backend (read-only).

Write and listing operations are infeasible over plain HTTP and raise
UnsupportedOperationError. read_range verifies the server honored the Range
header (206) and refuses to pass off a full-body 200 as a range.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from ondio.types import AuthError, ObjectNotFoundError, OndioError, UnsupportedOperationError

_CHUNK = 1024 * 1024


class HttpBackend:
    """Read-only StorageBackend for plain `http(s)://` URLs, via requests.

    See [`StorageBackend`][ondio.backends.StorageBackend] for method
    semantics. Requires the `http` extra. `upload`, `list_files`,
    `object_count`, and `delete` raise `UnsupportedOperationError` —
    infeasible over plain HTTP. `read_range` verifies the server honored the
    `Range` header (206) and refuses to pass off a full-body 200 as a range.
    """

    def __init__(self):
        try:
            import requests
        except ImportError as exc:
            raise ImportError(
                "http(s):// support requires the 'http' extra: pip install ondio[http]"
            ) from exc
        self._requests = requests
        self._local = threading.local()  # requests.Session is not thread-safe

    @property
    def _session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._local.session = self._requests.Session()
        return session

    def _check_status(self, uri: str, response) -> None:
        if response.status_code == 404:
            raise ObjectNotFoundError(f"no such object: {uri}")
        if response.status_code in (401, 403):
            raise AuthError(f"access denied ({response.status_code}): {uri}")
        if response.status_code >= 400:
            raise OndioError(f"HTTP {response.status_code} for {uri}")

    def read(self, uri: str) -> bytes:
        response = self._session.get(uri)
        self._check_status(uri, response)
        return response.content

    def read_range(self, uri: str, start_byte: int, end_byte: int | None) -> bytes:
        if start_byte < 0:
            raise ValueError(f"start_byte must be >= 0, got {start_byte}")
        if end_byte is not None and end_byte < start_byte:
            raise ValueError(f"end_byte {end_byte} < start_byte {start_byte}")
        header = f"bytes={start_byte}-" if end_byte is None else f"bytes={start_byte}-{end_byte}"
        response = self._session.get(uri, headers={"Range": header})
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
        response = self._session.head(uri, allow_redirects=True)
        self._check_status(uri, response)
        length = response.headers.get("Content-Length")
        if length is None:
            raise UnsupportedOperationError(f"server reports no Content-Length: {uri}")
        return int(length)

    def download(self, uri: str, out_path: str | os.PathLike[str]) -> None:
        dest = Path(out_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._session.get(uri, stream=True) as response:
            self._check_status(uri, response)
            with open(dest, "wb") as f:
                for chunk in response.iter_content(_CHUNK):
                    f.write(chunk)

    def exists(self, uri: str) -> bool:
        response = self._session.head(uri, allow_redirects=True)
        if response.status_code in (405, 501):  # server does not support HEAD
            with self._session.get(uri, stream=True) as get_response:
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
