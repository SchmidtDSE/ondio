"""Storage backends: one class per platform, each following `StorageBackend`.

The SDKs load only when a backend is constructed, so importing this package is light.
"""

from ondio.backends.aws import AwsBackend
from ondio.backends.gcs import GcsBackend
from ondio.backends.http import HttpBackend
from ondio.backends.local import LocalBackend
from ondio.backends.protocol import StorageBackend

__all__ = ["AwsBackend", "GcsBackend", "HttpBackend", "LocalBackend", "StorageBackend"]
