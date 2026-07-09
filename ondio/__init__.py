"""ondio: uniform IO for audio data across AWS S3, GCS, HTTP, and local files.

One URI-addressed interface; the platform is detected from the scheme and the
call is dispatched to the matching backend. See PROJECT.md for the full design.
"""

from ondio.dispatcher import (
    delete,
    download,
    #download_files,
    download_flac,
    download_json,
    exists,
    extract_flac_header,
    list_files,
    object_count,
    read,
    read_flac,
    upload,
    upload_file,
    upload_json,
)
from ondio.registry import detect_platform
from ondio.types import (
    AuthError,
    FlacHeader,
    ObjectNotFoundError,
    OndioError,
    UnknownPlatformError,
    UnsupportedOperationError,
)

__all__ = [
    "AuthError",
    "FlacHeader",
    "ObjectNotFoundError",
    "OndioError",
    "UnknownPlatformError",
    "UnsupportedOperationError",
    "delete",
    "detect_platform",
    "download",
    "download_flac",
    "download_json",
    "exists",
    "extract_flac_header",
    "list_files",
    "object_count",
    "read",
    "read_flac",
    "upload",
    "upload_file",
    "upload_json",
]
