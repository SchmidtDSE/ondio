"""The StorageBackend Protocol: byte-level primitives every platform implements.

Format-specific logic (FLAC parsing, JSON/parquet helpers) lives above this layer,
in the dispatcher and file-format specific modules — backends deal only in bytes and objects.
"""

from __future__ import annotations

import os
from typing import Protocol

class StorageBackend(Protocol):
    def read(self, uri: str) -> bytes:
        """Read the full object into memory. Equivalent to read_range(uri, 0, None)."""
        ...

    def read_range(self, uri: str, start_byte: int, end_byte: int | None) -> bytes:
        """Read an inclusive byte range; end_byte=None means "to EOF".

        Ranges extending past EOF return the available bytes (possibly empty).
        Raises UnsupportedOperationError if the backend cannot honor a range
        (e.g. an HTTP server).
        """
        ...

    def size(self, uri: str) -> int:
        """Object size in bytes, without fetching the object."""
        ...

    def upload(self, uri: str, data: bytes) -> None:
        """Write bytes to a URI."""
        ...

    def download(self, uri: str, out_path: str | os.PathLike[str]) -> None:
        """Stream an object to a local path, creating parent directories."""
        ...

    def list_files(
        self, uri_prefix: str, *, recursive: bool = True, max_items: int | None = None
    ) -> list[str]:
        """List full URIs under a prefix, lexicographically sorted.

        recursive=False lists only the immediate level. max_items caps the result;
        None means unbounded. A prefix with no matches yields an empty list.
        """
        ...

    def object_count(
        self,
        uri_prefix: str,
        *,
        required_prefix: str | None = None,
        required_ext: str | None = None,
    ) -> int:
        """Count objects under a prefix (always recursive) without building URI lists.

        required_prefix filters by filename prefix; required_ext by file extension
        (with or without the leading dot).
        """
        ...

    def exists(self, uri: str) -> bool:
        """True if the URI addresses an existing object (not a directory/prefix)."""
        ...

    def delete(self, uri: str) -> None:
        """Delete a single object. Idempotent: deleting a missing object is a no-op."""
        ...
