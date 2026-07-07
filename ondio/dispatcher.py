"""Top-level public functions: resolve the backend from a URI and delegate.

Every function accepts **kwargs, forwarded to the backend constructor (see
registry.get_backend), not to the individual operation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ondio.registry import get_backend


def read(uri: str, **kwargs: Any) -> bytes:
    """Read the full object at `uri` into memory."""
    return get_backend(uri, **kwargs).read(uri)

def read_json(uri: str, **kwargs: Any) -> Any:
    """Read and parse the JSON object at `uri`."""
    return json.loads(read(uri, **kwargs))

def download(uri: str, out_path: str | os.PathLike[str], **kwargs: Any) -> None:
    """Stream the object at `uri` to a local path, creating parent directories if the don't exist."""
    get_backend(uri, **kwargs).download(uri, out_path)

def write(uri: str, data: bytes, **kwargs: Any) -> None:
    """Write raw bytes to `uri`."""
    get_backend(uri, **kwargs).write(uri, data)

def write_json(uri: str, data: Any, **kwargs: Any) -> None:
    """Serialize `data` as JSON and upload it to `uri`."""
    write(uri, json.dumps(data, ensure_ascii=False).encode("utf-8"), **kwargs)

def upload(uri: str, source_path: str | os.PathLike[str], **kwargs: Any) -> None:
    """Upload a local file to `uri`."""
    write(uri, Path(source_path).read_bytes(), **kwargs)

def list_files(
    uri_prefix: str,
    *,
    recursive: bool = True,
    max_items: int | None = None,
    **kwargs: Any,
) -> list[str]:
    """List full URIs under a prefix, lexicographically sorted."""
    return get_backend(uri_prefix, **kwargs).list_files(
        uri_prefix, recursive=recursive, max_items=max_items
    )

def object_count(
    uri_prefix: str,
    *,
    required_prefix: str | None = None,
    required_ext: str | None = None,
    **kwargs: Any,
) -> int:
    """Count objects under a prefix (always recursive) without building URI lists."""
    return get_backend(uri_prefix, **kwargs).object_count(
        uri_prefix, required_prefix=required_prefix, required_ext=required_ext
    )

def exists(uri: str, **kwargs: Any) -> bool:
    """True if `uri` points to an existing object."""
    return get_backend(uri, **kwargs).exists(uri)

def delete(uri: str, **kwargs: Any) -> None:
    """Delete the object at `uri`. Idempotent: a missing object is a no-op."""
    get_backend(uri, **kwargs).delete(uri)


