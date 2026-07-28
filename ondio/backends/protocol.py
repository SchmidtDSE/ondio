"""The StorageBackend Protocol: byte-level primitives every platform implements.

Format-specific logic (FLAC parsing, JSON/parquet helpers) lives above this layer,
in the dispatcher and file-format specific modules — backends deal only in bytes and objects.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Protocol

from ondio.types import ObjectNotFoundError

def _matches_filters(
    name: str, required_prefix: str | None, required_ext: str | None
) -> bool:
    """Filename-level filter shared by `list_files`/`object_count` implementations."""
    if required_prefix is not None and not name.startswith(required_prefix):
        return False
    if required_ext is not None:
        ext = required_ext if required_ext.startswith(".") else f".{required_ext}"
        if not name.endswith(ext):
            return False
    return True


def _folder_match(key: str) -> tuple[str, str | None]:
    """Split a listing prefix into (folder prefix, exact key) for folder semantics.

    A prefix names a folder: it matches the object at that exact key plus
    everything under "key/" — never sibling keys that merely share leading
    characters ("results/run-1" does not match "results/run-10/x"). A prefix
    that is empty or ends in "/" names folder contents only.
    """
    if key and not key.endswith("/"):
        return f"{key}/", key
    return key, None


def _exact_object_matches(
    size: Callable[[str], int],
    uri: str,
    required_prefix: str | None,
    required_ext: str | None,
) -> bool:
    """Whether an object exists at exactly `uri` and passes the filename filters.

    The `exact_key` half of `_folder_match`, shared by the object-store
    backends: a native prefix listing cannot express "this key and everything
    below it, but not its siblings", so they list "key/" for the folder
    contents and probe the bare key separately.
    """
    if not _matches_filters(uri.rsplit("/", 1)[-1], required_prefix, required_ext):
        return False
    try:
        size(uri)
    except ObjectNotFoundError:
        return False
    return True


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

    def write(self, uri: str, data: bytes) -> None:
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

        The prefix has folder semantics on every backend: it matches the
        object at that exact path (if one exists) and every object under it
        as a folder, but never sibling objects that merely share leading
        characters — "results/run-1" matches "results/run-1" and
        "results/run-1/x" but not "results/run-10/x".

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

        Same folder semantics as `list_files` (an exact-path object counts,
        folder placeholder keys do not), so `object_count(p)` always equals
        `len(list_files(p))` given the same filters.

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
