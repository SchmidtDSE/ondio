"""Maps URI scheme to platform detection, backend factory registry"""

from __future__ import annotations

from importlib import import_module
from urllib.parse import urlparse

from ondio._log import printer
from ondio.backends.gcs import GcsBackend
from ondio.backends.protocol import StorageBackend
from ondio.backends.aws import _split as _split_s3
from ondio.backends.gcs import _split as _split_gcs
from ondio.backends.local import _to_path
from ondio.types import Location, UnknownPlatformError, UnsupportedOperationError

_SCHEME_TO_PLATFORM = {
    "s3": "aws",
    "gs": "gcs",
    "http": "url",
    "https": "url",
    "file": "local",
}

_BACKEND_FACTORIES = {
    "local": ("ondio.backends.local", "LocalBackend"),
    "aws": ("ondio.backends.aws", "AwsBackend"),
    "url": ("ondio.backends.http", "HttpBackend"),
    "gcs": ("ondio.backends.gcs", "GcsBackend"), 
}

def detect_platform(uri: str) -> str:
    """Maps a URI to its platform based on its scheme"""
    scheme = urlparse(uri).scheme.lower()
    if len(scheme) <= 1:  # no scheme, or a drive letter (e.g. C:/) — path-like, hence try local
        return "local"
    try:
        return _SCHEME_TO_PLATFORM[scheme]
    except KeyError:
        raise UnknownPlatformError(
            f"no platform registered for scheme {scheme!r} (from {uri!r})"
        ) from None


def locate(uri: str) -> Location:
    """The store `uri` is in, and the path in that store that ondio reads.

    It uses the same parsing as reads and writes, so it does not touch storage.
    Local `file:` paths are percent-decoded, as reads decode them; S3 and GCS keys
    are used as written.

    Raises:
        ValueError: For a relative local path, a `file://` URI with a remote host,
            or an `s3://` or `gs://` URI with no bucket.
        UnsupportedOperationError: For an HTTP URL, which ondio cannot write.
        UnknownPlatformError: For an unknown scheme.
    """
    platform = detect_platform(uri)
    if platform == "aws":
        bucket, key = _split_s3(uri)
        return Location(f"s3://{bucket}", key)
    if platform == "gcs":
        bucket, key = _split_gcs(uri)
        return Location(f"gs://{bucket}", key)
    if platform == "local":
        path = _to_path(uri)
        if not path.is_absolute():
            raise ValueError(f"not an absolute local path: {uri!r}")
        # removeprefix, not lstrip: a path that starts with `//` keeps an empty first
        # segment, which a caller can refuse.
        return Location("file://", path.as_posix().removeprefix("/"))
    raise UnsupportedOperationError(f"no fixed storage location for {uri!r}")


def get_backend(uri: str, **kwargs) -> StorageBackend:
    """Resolve the backend for a given URI. kwargs are forwarded to the backend constructor."""
    platform = detect_platform(uri)
    printer.message(f"using {platform} backend for {uri}")
    module_name, class_name = _BACKEND_FACTORIES[platform]
    factory = getattr(import_module(module_name), class_name) 
    backend = factory(**kwargs)
    return backend
