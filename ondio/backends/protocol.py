"""The StorageBackend Protocol: byte-level primitives every platform implements.

Format-specific logic (FLAC parsing, JSON/parquet helpers) lives above this layer,
in the dispatcher and file-format specific modules — backends deal only in bytes and objects.
"""

from __future__ import annotations

import os
from typing import Protocol


class StorageBackend(Protocol):
    """Byte-level storage primitives, implemented once per platform.

    Implementations translate their platform SDK's errors into ondio's
    exception hierarchy (`ObjectNotFoundError`, `AuthError`,
    `UnsupportedOperationError`, `OndioError`). All URIs are fully qualified
    and addressable by the backend they are passed to.
    """

    def read(self, uri: str) -> bytes:
        """Read the full object into memory.

        Equivalent to `read_range(uri, 0, None)`.

        Args:
            uri: URI of the object.

        Returns:
            The object's bytes.

        Raises:
            ObjectNotFoundError: If the object does not exist.
        """
        ...

    def read_range(self, uri: str, start_byte: int, end_byte: int | None) -> bytes:
        """Read an inclusive byte range.

        Args:
            uri: URI of the object.
            start_byte: First byte to read (>= 0).
            end_byte: Last byte to read (inclusive); None means "to EOF".

        Returns:
            The requested bytes. Ranges extending past EOF return the
            available bytes (possibly empty).

        Raises:
            ValueError: If the range is negative or inverted.
            ObjectNotFoundError: If the object does not exist.
            UnsupportedOperationError: If the backend cannot honor a range
                (e.g. an HTTP server that ignores `Range` and answers 200).
        """
        ...

    def size(self, uri: str) -> int:
        """Object size in bytes, without fetching the object.

        Args:
            uri: URI of the object.

        Returns:
            The size in bytes (S3 HeadObject, GCS blob metadata, HTTP HEAD
            Content-Length, local stat).

        Raises:
            ObjectNotFoundError: If the object does not exist.
        """
        ...

    def upload(self, uri: str, data: bytes) -> None:
        """Write bytes to a URI.

        Args:
            uri: Destination URI.
            data: The bytes to write.
        """
        ...

    def download(self, uri: str, out_path: str | os.PathLike[str]) -> None:
        """Stream an object to a local path, creating parent directories.

        Args:
            uri: URI of the object.
            out_path: Local destination path.

        Raises:
            ObjectNotFoundError: If the object does not exist.
        """
        ...

    def list_files(
        self,
        uri_prefix: str,
        *,
        recursive: bool = True,
        max_items: int | None = None,
        required_prefix: str | None = None,
        required_ext: str | None = None,
    ) -> list[str]:
        """List full URIs (not bare keys) under a prefix, lexicographically sorted.

        Args:
            uri_prefix: Prefix to list under.
            recursive: If False, list only the immediate level.
            max_items: Cap on the number of results, applied after the
                filename filters; None means unbounded.
            required_prefix: If given, list only objects whose *filename*
                starts with this.
            required_ext: If given, list only objects with this file
                extension (with or without the leading dot).

        Returns:
            Sorted full URIs; empty if nothing matches.
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

        Args:
            uri_prefix: Prefix to count under.
            required_prefix: If given, count only objects whose *filename*
                starts with this.
            required_ext: If given, count only objects with this file
                extension (with or without the leading dot).

        Returns:
            The number of matching objects.
        """
        ...

    def exists(self, uri: str) -> bool:
        """Whether the URI addresses an existing object.

        Args:
            uri: URI to check.

        Returns:
            True if the object exists (not a directory/prefix).
        """
        ...

    def delete(self, uri: str) -> None:
        """Delete a single object. Idempotent: deleting a missing object is a no-op.

        Args:
            uri: URI of the object.
        """
        ...
