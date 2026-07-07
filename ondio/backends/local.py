"""Local-filesystem backend."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlparse


def _to_path(uri: str) -> Path:
    if uri.startswith("file://"):
        parsed = urlparse(uri)
        if parsed.netloc not in ("", "localhost"):
            raise ValueError(f"file:// URIs with a remote host are not supported: {uri!r}")
        return Path(unquote(parsed.path))
    return Path(uri)


class LocalBackend:
    def read(self, uri: str) -> bytes:
        try:
            return _to_path(uri).read_bytes()
        except OSError as exc:
            raise exc

    def read_range(self, uri: str, start_byte: int, end_byte: int | None) -> bytes:
        pass

    def size(self, uri: str) -> int:
        pass 

    def write(self, uri: str, data : bytes) -> None:
        pass

    def download(self, uri: str, out_path: str | os.PathLike[str]) -> None:
        pass 

    def list_files(
            self, uri_prefix: str, *, recursive: bool = True, max_items: int | None = None
    ) -> list[str]:
        pass 

    def object_count(
        self,
        uri_prefix: str,
        *,
        required_prefix: str | None = None,
        required_ext: str | None = None,
    ) -> int:
       pass 

    def exists(self, uri: str) -> bool:
        pass

    def delete(self, uri: str) -> None:
        pass 
