"""Maps URI scheme to platform detection, backend factory registry"""

from __future__ import annotations

from importlib import import_module
from urllib.parse import urlparse

from ondio.backends.gcs import GcsBackend
from ondio.types import UnknownPlatformError
from ondio.backends.protocol import StorageBackend

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


def get_backend(uri: str, **kwargs) -> StorageBackend:
    """Resolve the backend for a given URI. kwargs are forwarded to the backend constructor."""
    platform = detect_platform(uri)
    module_name, class_name = _BACKEND_FACTORIES[platform]
    factory = getattr(import_module(module_name), class_name) 
    backend = factory(**kwargs)
    return backend
