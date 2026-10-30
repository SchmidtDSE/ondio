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
        ValueError: For a relative local path, a `file:` URI that does not start
            with `file://`, a `file://` URI with a remote host, an `s3://` or `gs://`
            URI with no bucket, or a `file:`, `s3://` or `gs://` URI with `?` or `#`.
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


# For each platform, the environment variable that carries each option to another
# process. An option that is not here cannot be carried.
_OPTION_VARIABLES = {
    "aws": {"endpoint_url": "AWS_ENDPOINT_URL_S3"},
}


def get_environment(uri: str, **kwargs) -> dict[str, str]:
    """The environment variables that carry options to another process.

    When these variables are set in another process, its ondio calls on `uri`, made
    with no options, act as calls made with `kwargs` here. The variables apply to
    every call on the same platform in that process, not only to calls on `uri`.
    An option set to None gives no variable. It does not touch storage or load an SDK.

    That process must not turn the variables off: if it sets
    `AWS_IGNORE_CONFIGURED_ENDPOINT_URLS`, or its AWS config sets
    `ignore_configured_endpoint_urls`, S3 calls use the AWS endpoint, with no error.

    Raises:
        UnsupportedOperationError: For an option that no variable carries on this
            platform, such as `region_name`, or `endpoint_url` on GCS. The message
            names the option, not its value, because a value can hold a secret.
        UnknownPlatformError: For an unknown scheme.
    """
    platform = detect_platform(uri)
    # A backend treats None as its default, so None needs no variable.
    options = {name: value for name, value in kwargs.items() if value is not None}
    variables = _OPTION_VARIABLES.get(platform, {})
    refused = sorted(name for name in options if name not in variables)
    if refused:
        raise UnsupportedOperationError(
            f"no environment variable carries {', '.join(refused)} on {platform}"
        )
    return {variables[name]: value for name, value in options.items()}


def get_backend(uri: str, **kwargs) -> StorageBackend:
    """Resolve the backend for a given URI. kwargs are forwarded to the backend constructor."""
    platform = detect_platform(uri)
    printer.message(f"using {platform} backend for {uri}")
    module_name, class_name = _BACKEND_FACTORIES[platform]
    factory = getattr(import_module(module_name), class_name) 
    backend = factory(**kwargs)
    return backend
