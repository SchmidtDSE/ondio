"""Core data types and custom exceptions for ondio"""

from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class FlacHeader:
    """ STREAMINFO metadata of a FLAC file/stream. """

    sample_rate: int
    channels: int
    bits_per_sample: int
    total_samples: int
    md5: bytes
    audio_data_offset: int

    @property
    def duration(self) -> float:
        """Stream duration in seconds (0.0 if sample_rate is 0)."""
        if self.sample_rate == 0:
            return 0.0
        return self.total_samples / self.sample_rate


class OndioError(Exception):
    """Base class for all ondio errors."""


class UnknownPlatformError(OndioError):
    """The URI scheme does not map to any registered platform."""


class UnsupportedOperationError(OndioError):
    """The backend cannot perform the requested operation (e.g. list_files over HTTP)."""


class ObjectNotFoundError(OndioError):
    """The object addressed by the URI does not exist."""


class ObjectExistsError(OndioError):
    """A create-only write found an object already at the URI. The object is unchanged."""


class AuthError(OndioError):
    """The backend rejected the request for authentication/authorization reasons."""



