"""`ondio`: uniform IO for audio data across AWS S3, GCS, HTTP, and local files.

One URI-addressed interface; the platform is detected from the scheme and the
call is dispatched to the matching backend. See the [architecture][architecture]
page for the full design.
"""

from ondio.dispatcher import (
    create,
    delete,
    download,
    download_files,
    download_flac,
    exists,
    extract_flac_header,
    list_files,
    object_count,
    read,
    read_flac,
    read_json,
    read_range,
    upload,
    upload_files,
    write,
    write_json,
)
from ondio._log import set_printer
from ondio.registry import detect_platform, get_backend, get_environment, locate
from ondio.types import (
    AuthError,
    FlacHeader,
    Location,
    NetworkError,
    ObjectExistsError,
    ObjectNotFoundError,
    OndioError,
    UnknownPlatformError,
    UnsupportedOperationError,
)

__all__ = [
    "AuthError",
    "FlacHeader",
    "Location",
    "NetworkError",
    "ObjectExistsError",
    "ObjectNotFoundError",
    "OndioError",
    "UnknownPlatformError",
    "UnsupportedOperationError",
    "create",
    "delete",
    "detect_platform",
    "download",
    "download_files",
    "download_flac",
    "exists",
    "extract_flac_header",
    "get_backend",
    "get_environment",
    "list_files",
    "locate",
    "object_count",
    "read",
    "read_flac",
    "read_json",
    "read_range",
    "set_printer",
    "upload",
    "upload_files",
    "write",
    "write_json",
]
