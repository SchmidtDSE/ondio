"""Top-level public functions: resolve the backend from a URI and delegate.

Every function accepts `**kwargs`, forwarded to the backend constructor (see
[`get_backend`][ondio.registry.get_backend]) — not to the individual operation.
URIs are fully qualified: `s3://…`, `gs://…`, `http(s)://…`, `file://…`, or a
plain local path.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Literal, Sequence, overload
from urllib.parse import urlparse

import numpy as np

from ondio import flac as _flac
from ondio._log import printer
from ondio.registry import get_backend
from ondio.types import FlacHeader, OndioError

def read(uri: str, **kwargs: Any) -> bytes:
    """Read the full object at `uri` into memory.

    Args:
        uri: Fully-qualified storage URI.
        **kwargs: Forwarded to the backend constructor.

    Returns:
        The object's bytes.
    """
    return get_backend(uri, **kwargs).read(uri)


def download(uri: str, out_path: str | os.PathLike[str], **kwargs: Any) -> None:
    """Stream the object at `uri` to a local path, creating parent directories.

    Args:
        uri: Fully-qualified storage URI.
        out_path: Local destination path; parent directories are created.
        **kwargs: Forwarded to the backend constructor.
    """
    get_backend(uri, **kwargs).download(uri, out_path)
    printer.message(f"downloaded {uri} to {out_path}")


def download_files(
    uris: Sequence[str],
    local_dir: str | os.PathLike[str],
    *,
    max_workers: int | None = None,
    **kwargs: Any,
) -> list[str]:
    """Download each object in `uris` into `local_dir`, named by its basename.

    Downloads run concurrently, and each URI dispatches independently, so
    schemes may be mixed in one call. The returned paths always follow the
    input order. The first failure cancels downloads not yet started and
    re-raises; files that already finished are left in place.

    (Counterpart of legacy soundhub_utils `io.download_files(uris, local_dir)`
    — with one deliberate change: URIs whose basenames collide raise upfront
    instead of silently overwriting each other in `local_dir`.)

    Args:
        uris: Fully-qualified storage URIs; schemes may be mixed.
        local_dir: Local directory to download into, created if missing.
            When `uris` is empty nothing happens and the directory is not
            created.
        max_workers: Thread-pool size for the concurrent downloads.
        **kwargs: Forwarded to the backend constructor.

    Returns:
        Local paths of the downloaded files, in the order of `uris`.

    Raises:
        ValueError: Before anything is downloaded — if a URI has an empty
            basename (a prefix-like URI), or if two URIs share a basename.
    """
    if not uris:
        return []

    jobs: list[tuple[str, Path]] = []
    seen: dict[str, str] = {}
    for uri in uris:
        # Path.name would strip a trailing slash ("/prefix/" -> "prefix"), so
        # treat those as prefix-like explicitly.
        path_part = urlparse(uri).path
        name = "" if path_part.endswith("/") else Path(path_part).name
        if not name:
            raise ValueError(f"cannot derive a filename from {uri!r}")
        if name in seen:
            raise ValueError(
                f"duplicate basename {name!r}: {seen[name]!r} and {uri!r} "
                f"would overwrite each other in {os.fspath(local_dir)!r}"
            )
        seen[name] = uri
        jobs.append((uri, Path(local_dir) / name))

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(download, uri, path, **kwargs) for uri, path in jobs]
        try:
            for future in as_completed(futures):
                future.result()
        except BaseException:
            executor.shutdown(wait=True, cancel_futures=True)
            raise
    printer.message(f"downloaded {len(jobs)} files to {os.fspath(local_dir)}")
    return [str(path) for _, path in jobs]


def write(uri: str, data: bytes, **kwargs: Any) -> None:
    """Write raw bytes to `uri`.

    Args:
        uri: Fully-qualified storage URI to write to.
        data: The bytes to write.
        **kwargs: Forwarded to the backend constructor.
    """
    get_backend(uri, **kwargs).write(uri, data)


def write_json(uri: str, data: Any, **kwargs: Any) -> None:
    """Serialize `data` as JSON (UTF-8, `ensure_ascii=False`) and upload it to `uri`.

    Args:
        uri: Fully-qualified storage URI to write to.
        data: Any `json.dumps`-serializable value.
        **kwargs: Forwarded to the backend constructor.
    """
    write(uri, json.dumps(data, ensure_ascii=False).encode("utf-8"), **kwargs)


def download_json(uri: str, **kwargs: Any) -> Any:
    """Download and parse the JSON object at `uri`.

    Args:
        uri: Fully-qualified storage URI of a JSON document.
        **kwargs: Forwarded to the backend constructor.

    Returns:
        The parsed JSON value (dict, list, str, number, bool, or None).
    """
    return json.loads(read(uri, **kwargs))


def upload(uri: str, source_path: str | os.PathLike[str], **kwargs: Any) -> None:
    """Upload a local file to `uri`.

    Args:
        uri: Fully-qualified storage URI to write to.
        source_path: Local file to upload.
        **kwargs: Forwarded to the backend constructor.
    """
    write(uri, Path(source_path).read_bytes(), **kwargs)
    printer.message(f"uploaded {source_path} to {uri}")


def list_files(
    uri_prefix: str,
    *,
    recursive: bool = True,
    max_items: int | None = None,
    required_prefix: str | None = None,
    required_ext: str | None = None,
    **kwargs: Any,
) -> list[str]:
    """List full URIs under a prefix, lexicographically sorted.

    Args:
        uri_prefix: Prefix to list under (a "directory" URI or a partial
            object name, S3-style).
        recursive: If False, list only the immediate level.
        max_items: Cap on the number of results, applied after the filename
            filters; None means unbounded.
        required_prefix: If given, list only objects whose *filename* starts
            with this (applied to every recursively listed key).
        required_ext: If given, list only objects with this file extension
            (with or without the leading dot).
        **kwargs: Forwarded to the backend constructor.

    Returns:
        Full URIs (never bare keys), sorted; empty if nothing matches.
    """
    return get_backend(uri_prefix, **kwargs).list_files(
        uri_prefix,
        recursive=recursive,
        max_items=max_items,
        required_prefix=required_prefix,
        required_ext=required_ext,
    )

def object_count(
    uri_prefix: str,
    *,
    required_prefix: str | None = None,
    required_ext: str | None = None,
    **kwargs: Any,
) -> int:
    """Count objects under a prefix (always recursive) without building URI lists.

    Cheaper than `len(list_files(...))` when only a count is needed.

    Args:
        uri_prefix: Prefix to count under.
        required_prefix: If given, count only objects whose *filename* starts
            with this (applied to every recursively listed key).
        required_ext: If given, count only objects with this file extension
            (with or without the leading dot).
        **kwargs: Forwarded to the backend constructor.

    Returns:
        The number of matching objects.
    """
    return get_backend(uri_prefix, **kwargs).object_count(
        uri_prefix, required_prefix=required_prefix, required_ext=required_ext
    )


def exists(uri: str, **kwargs: Any) -> bool:
    """Whether `uri` addresses an existing object.

    Args:
        uri: Fully-qualified storage URI.
        **kwargs: Forwarded to the backend constructor.

    Returns:
        True if the object exists (directories/prefixes do not count).
    """
    return get_backend(uri, **kwargs).exists(uri)


def delete(uri: str, **kwargs: Any) -> None:
    """Delete the object at `uri`. Idempotent: a missing object is a no-op.

    Args:
        uri: Fully-qualified storage URI.
        **kwargs: Forwarded to the backend constructor.
    """
    get_backend(uri, **kwargs).delete(uri)


def extract_flac_header(uri: str, **kwargs: Any) -> FlacHeader:
    """Parse the STREAMINFO of the FLAC file at `uri`, fetching only header bytes.

    Metadata block bodies (e.g. embedded artwork) are skipped, not downloaded.

    Args:
        uri: Fully-qualified storage URI of a FLAC file.
        **kwargs: Forwarded to the backend constructor.

    Returns:
        The parsed [`FlacHeader`][ondio.types.FlacHeader].

    Raises:
        OndioError: If `uri` is not a valid FLAC stream.
    """
    return _flac.extract_flac_header(get_backend(uri, **kwargs), uri)


@overload
def read_flac(
    uri: str,
    start_sec: float | None = None,
    end_sec: float | None = None,
    *,
    decode: Literal[True] = True,
    padding_ratio: float = 0.25,
    use_range: bool | None = None,
    **kwargs: Any,
) -> tuple[np.ndarray, int]: ...


@overload
def read_flac(
    uri: str,
    start_sec: float | None = None,
    end_sec: float | None = None,
    *,
    decode: Literal[False],
    padding_ratio: float = 0.25,
    use_range: bool | None = None,
    **kwargs: Any,
) -> bytes: ...


def read_flac(
    uri: str,
    start_sec: float | None = None,
    end_sec: float | None = None,
    *,
    decode: bool = True,
    padding_ratio: float = 0.25,
    use_range: bool | None = None,
    **kwargs: Any,
) -> tuple[np.ndarray, int] | bytes:
    """Read a FLAC file — whole, or a `[start_sec, end_sec]` window — into memory.

    Decoding shells out to the ffmpeg CLI, which must be on PATH; the
    whole-file `decode=False` fast path never invokes it.

    Args:
        uri: Fully-qualified storage URI of a FLAC file.
        start_sec: Window start in seconds; None means from the beginning.
        end_sec: Window end in seconds; None means to the end of the stream
            (values past EOF are clamped, like Python slices).
        decode: If True (default), return decoded PCM; if False, return the
            bytes of a standalone FLAC file — the original bytes exactly when
            no window is given (no ffmpeg involved), a fresh lossless encode
            of the slice otherwise.
        padding_ratio: Fraction of the window duration fetched as extra
            padding on each side of the ranged download (ignored on the
            full-download path).
        use_range: Fetch strategy. None (default) picks automatically: ranged
            download for files > 50 MB when the window is < 1/3 of the stream,
            full download otherwise. True/False forces one path.
        **kwargs: Forwarded to the backend constructor.

    Returns:
        With `decode=True`, `(samples, sample_rate)` where `samples` is a
        float32 array of shape `(frames, channels)` normalized to `[-1, 1]`
        like libsndfile/soundfile. With `decode=False`, FLAC bytes.

    Raises:
        ValueError: If the window is empty, negative, or starts past EOF.
        OndioError: If `uri` is not a valid FLAC stream, or the stream
            reports zero duration and a window was requested.

    Warning:
        The ranged path estimates byte positions linearly from the header's
        duration: the slice is aligned only approximately (tens of ms), and a
        header that lies about the duration (truncated recorder files exist)
        yields audio from the wrong position with no error. Pass
        `use_range=False` when the header cannot be trusted.
    """
    return _flac.read_flac(
        get_backend(uri, **kwargs), uri, start_sec, end_sec,
        decode=decode, padding_ratio=padding_ratio, use_range=use_range,
    )


def download_flac(
    uri: str,
    out_path: str | os.PathLike[str],
    start_sec: float | None = None,
    end_sec: float | None = None,
    *,
    padding_ratio: float = 0.25,
    use_range: bool | None = None,
    **kwargs: Any,
) -> None:
    """Download a FLAC file — whole, or a `[start_sec, end_sec]` window — to a local path.

    With no window this is a plain byte-for-byte download (no ffmpeg). With
    one, the window is decoded with ffmpeg, sliced at millisecond granularity,
    and re-encoded — losslessly identical PCM, but not byte-identical to the
    source. Window and fetch semantics, requirements, and the ranged-path
    warning are as in [`read_flac`][ondio.dispatcher.read_flac].

    (Counterpart of legacy soundhub_utils `io.read_flac(src, dest, ...)`.)

    Args:
        uri: Fully-qualified storage URI of a FLAC file.
        out_path: Local `.flac` destination path; parent directories are created.
        start_sec: Window start in seconds; None means from the beginning.
        end_sec: Window end in seconds; None means to the end of the stream.
        padding_ratio: As in [`read_flac`][ondio.dispatcher.read_flac].
        use_range: As in [`read_flac`][ondio.dispatcher.read_flac].
        **kwargs: Forwarded to the backend constructor.

    Raises:
        ValueError: If the window is empty, negative, or starts past EOF.
        OndioError: If `uri` is not a valid FLAC stream, or the stream
            reports zero duration and a window was requested.
    """
    _flac.download_flac(
        get_backend(uri, **kwargs), uri, out_path, start_sec, end_sec,
        padding_ratio=padding_ratio, use_range=use_range,
    )


