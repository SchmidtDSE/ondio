"""Top-level public functions: resolve the backend from a URI and delegate.

Every function accepts `**kwargs`, forwarded to the backend constructor (see
[`get_backend`][ondio.registry.get_backend]) — not to the individual operation.
Every function also accepts `backend=`, a backend to use instead of one built from
the URI: build it once and pass it to many calls to reuse its client and connections.
URIs are fully qualified: `s3://…`, `gs://…`, `http(s)://…`, `file://…`, or a
plain local path. A `file:`, `s3://` or `gs://` URI with `?` or `#` is refused with
`ValueError`, since the text after it would be dropped. A local file whose name has
`?` or `#` is reached with `%3F` and `%23` in a `file://` URI, or with a plain local
path, which is used as written. S3 and GCS keys are not decoded, so a key with `?`
or `#` cannot be reached.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Literal, Sequence, overload
from urllib.parse import quote, urlparse

import numpy as np

from ondio import flac as _flac
from ondio._log import printer
from ondio.backends.protocol import StorageBackend
from ondio.registry import get_backend
from ondio.types import FlacHeader, OndioError


def _refuse_options(backend: StorageBackend | None, kwargs: dict[str, Any]) -> None:
    # The options would be ignored, so a caller who gives both has made a mistake.
    if backend is not None and kwargs:
        raise ValueError(f"give backend or {', '.join(sorted(kwargs))}, not both")


def _resolve(uri: str, backend: StorageBackend | None, kwargs: dict[str, Any]) -> StorageBackend:
    """`backend` if given, else the backend that `get_backend` builds for `uri`."""
    _refuse_options(backend, kwargs)
    return backend if backend is not None else get_backend(uri, **kwargs)


def read(uri: str, *, backend: StorageBackend | None = None, **kwargs: Any) -> bytes:
    """Read the full object at `uri` into memory.

    Args:
        uri: Fully-qualified storage URI.
        backend: A backend to use instead of one built from `uri` and `kwargs`.
            Build it once and pass it to many calls to reuse its client and
            connections.
        **kwargs: Forwarded to the backend constructor.

    Returns:
        The object's bytes.
    """
    return _resolve(uri, backend, kwargs).read(uri)


def read_range(
    uri: str,
    start_byte: int,
    end_byte: int | None = None,
    *,
    backend: StorageBackend | None = None,
    **kwargs: Any,
) -> bytes:
    """Read the bytes from `start_byte` to `end_byte` (inclusive) of the object at `uri`.

    The range rules are those of
    [`StorageBackend.read_range`][ondio.backends.StorageBackend.read_range]:
    `end_byte=None` reads to the end, and a range past the end returns the
    bytes that exist, possibly none.

    Returns the stored bytes, not a playable file. For a time window of a FLAC
    file, use [`read_flac`][ondio.dispatcher.read_flac].

    Args:
        uri: Fully-qualified storage URI.
        start_byte: First byte to read (>= 0).
        end_byte: Last byte to read (inclusive); `None` means to the end.
        backend: A backend to use instead of one built from `uri` and `kwargs`.
            Build it once and pass it to many calls to reuse its client and
            connections.
        **kwargs: Forwarded to the backend constructor.

    Returns:
        The bytes of the range.

    Raises:
        ValueError: If the range is negative or inverted.
        UnsupportedOperationError: If the backend cannot read a range, e.g. an
            HTTP server that ignores `Range`.
    """
    return _resolve(uri, backend, kwargs).read_range(uri, start_byte, end_byte)


def download(
    uri: str,
    out_path: str | os.PathLike[str],
    *,
    backend: StorageBackend | None = None,
    **kwargs: Any,
) -> None:
    """Stream the object at `uri` to a local path, creating parent directories.

    Args:
        uri: Fully-qualified storage URI.
        out_path: Local destination path; parent directories are created.
        backend: A backend to use instead of one built from `uri` and `kwargs`.
            Build it once and pass it to many calls to reuse its client and
            connections.
        **kwargs: Forwarded to the backend constructor.
    """
    _resolve(uri, backend, kwargs).download(uri, out_path)
    printer.message(f"downloaded {uri} to {out_path}")


def download_files(
    uris: Sequence[str],
    local_dir: str | os.PathLike[str],
    *,
    max_workers: int | None = None,
    backend: StorageBackend | None = None,
    **kwargs: Any,
) -> list[str]:
    """Download each object in `uris` into `local_dir`, named by its basename.

    Downloads run concurrently, and each URI dispatches independently, so
    schemes may be mixed in one call. The returned paths always follow the
    input order. The first failure cancels downloads not yet started and
    re-raises; files that already finished are left in place.

    (Counterpart of legacy `soundhub_utils` `io.download_files(uris, local_dir)`
    — with one deliberate change: URIs whose basenames collide raise upfront
    instead of silently overwriting each other in `local_dir`.)

    Args:
        uris: Fully-qualified storage URIs; schemes may be mixed.
        local_dir: Local directory to download into, created if missing.
            When `uris` is empty nothing happens and the directory is not
            created.
        max_workers: Thread-pool size for the concurrent downloads.
        backend: A backend to use for every URI instead of one built per URI.
            All threads share it. S3 clients and `HttpBackend` are safe to
            share; a `google-cloud-storage` client does not promise it.
        **kwargs: Forwarded to the backend constructor.

    Returns:
        Local paths of the downloaded files, in the order of `uris`.

    Raises:
        ValueError: Before anything is downloaded — if a URI has an empty
            basename (a prefix-like URI), or if two URIs share a basename.
    """
    _refuse_options(backend, kwargs)
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
        futures = [
            executor.submit(download, uri, path, backend=backend, **kwargs) for uri, path in jobs
        ]
        try:
            for future in as_completed(futures):
                future.result()
        except BaseException:
            executor.shutdown(wait=True, cancel_futures=True)
            raise
    printer.message(f"downloaded {len(jobs)} files to {os.fspath(local_dir)}")
    return [str(path) for _, path in jobs]


def write(uri: str, data: bytes, *, backend: StorageBackend | None = None, **kwargs: Any) -> None:
    """Write raw bytes to `uri`.

    Args:
        uri: Fully-qualified storage URI to write to.
        data: The bytes to write.
        backend: A backend to use instead of one built from `uri` and `kwargs`.
            Build it once and pass it to many calls to reuse its client and
            connections.
        **kwargs: Forwarded to the backend constructor.
    """
    _resolve(uri, backend, kwargs).write(uri, data)


def create(uri: str, data: bytes, *, backend: StorageBackend | None = None, **kwargs: Any) -> None:
    """Write raw bytes to `uri` only if no object exists there.

    A reader sees either no object or the complete one, never part of it. Supported
    on S3, GCS, and local disk.

    Args:
        uri: Fully-qualified storage URI to write to.
        data: The bytes to write.
        backend: A backend to use instead of one built from `uri` and `kwargs`.
            Build it once and pass it to many calls to reuse its client and
            connections.
        **kwargs: Forwarded to the backend constructor.

    Raises:
        ObjectExistsError: If an object exists at `uri`. It is not changed. On S3
            and GCS it can be the object that this call wrote, if the client
            retried after a lost response: compare its bytes to know who wrote it.
        UnsupportedOperationError: Over HTTP (`HttpBackend` refuses it).
        OndioError: For any other failure. This does not prove that nothing was
            written: read `uri` to find out.
    """
    _resolve(uri, backend, kwargs).create(uri, data)


def write_json(
    uri: str, data: Any, *, backend: StorageBackend | None = None, **kwargs: Any
) -> None:
    """Serialize `data` as JSON (UTF-8, `ensure_ascii=False`) and upload it to `uri`.

    Args:
        uri: Fully-qualified storage URI to write to.
        data: Any `json.dumps`-serializable value.
        backend: A backend to use instead of one built from `uri` and `kwargs`.
            Build it once and pass it to many calls to reuse its client and
            connections.
        **kwargs: Forwarded to the backend constructor.
    """
    write(uri, json.dumps(data, ensure_ascii=False).encode("utf-8"), backend=backend, **kwargs)


def read_json(uri: str, *, backend: StorageBackend | None = None, **kwargs: Any) -> Any:
    """Read and parse the JSON object at `uri`.

    Args:
        uri: Fully-qualified storage URI of a JSON document.
        backend: A backend to use instead of one built from `uri` and `kwargs`.
            Build it once and pass it to many calls to reuse its client and
            connections.
        **kwargs: Forwarded to the backend constructor.

    Returns:
        The parsed JSON value (`dict`, `list`, `str`, number, `bool`, or `None`).
    """
    return json.loads(read(uri, backend=backend, **kwargs))


def upload(
    uri: str,
    source_path: str | os.PathLike[str],
    *,
    overwrite: bool = True,
    backend: StorageBackend | None = None,
    **kwargs: Any,
) -> None:
    """Upload a local file to `uri`, streaming it from disk.

    The file is sent without being read into memory. An existing object at
    `uri` is replaced, unless `overwrite=False`: the file is then written only
    if no object exists there, and a reader sees either no object or the
    complete one. Supported on S3, GCS, and local disk. On S3 it
    is a single PUT, so the file can be at most 5 GiB.

    Args:
        uri: Fully-qualified storage URI to write to.
        source_path: Local file to upload.
        overwrite: Replace an existing object. `False` refuses to.
        backend: A backend to use instead of one built from `uri` and `kwargs`.
            Build it once and pass it to many calls to reuse its client and
            connections.
        **kwargs: Forwarded to the backend constructor.

    Raises:
        FileNotFoundError: If `source_path` is not an existing file.
        ObjectExistsError: With `overwrite=False`, if an object exists at `uri`.
            It is not changed. On S3 and GCS it can be the object that this call
            wrote, if the client retried after a lost response: compare its
            bytes to know who wrote it.
        UnsupportedOperationError: Over HTTP, or with `overwrite=False` for a
            file larger than 5 GiB on S3.
        OndioError: For any other failure. With `overwrite=False` this does not
            prove that nothing was written: read `uri` to find out.
    """
    # _resolve also refuses options, but a ValueError must come before a FileNotFoundError.
    _refuse_options(backend, kwargs)
    if not Path(source_path).is_file():
        raise FileNotFoundError(f"no such file to upload: {source_path}")
    resolved = _resolve(uri, backend, kwargs)
    # Only when asked: a backend written before `overwrite` existed still works.
    if overwrite:
        resolved.upload(uri, source_path)
    else:
        resolved.upload(uri, source_path, overwrite=False)
    printer.message(f"uploaded {source_path} to {uri}")


def upload_files(
    uri_prefix: str,
    local_dir: str | os.PathLike[str],
    *,
    overwrite: bool = True,
    max_workers: int | None = None,
    backend: StorageBackend | None = None,
    **kwargs: Any,
) -> list[str]:
    """Upload every file under `local_dir` to `uri_prefix`, keeping relative paths.

    The counterpart of `download_files`. `local_dir/sub/a.bin` goes to
    `<uri_prefix>/sub/a.bin`; a trailing `/` on `uri_prefix` makes no
    difference. Every file is uploaded, hidden files included. Empty folders
    make no objects. A link to a file is uploaded with the content it points
    to; a link to a folder is not entered.

    Uploads run concurrently, in no fixed order. The first failure cancels
    uploads not yet started and re-raises; objects that already finished are
    left in place.

    Args:
        uri_prefix: Fully-qualified storage URI of the folder to upload into.
        local_dir: Local folder to upload.
        overwrite: Replace existing objects. `False` uploads each file only if
            no object exists at its URI, as `upload(..., overwrite=False)` does.
        max_workers: Thread-pool size for the concurrent uploads.
        backend: A backend to use for every file instead of one built per file.
            All threads share it. S3 clients are safe to share; a
            `google-cloud-storage` client does not promise it.
        **kwargs: Forwarded to the backend constructor.

    Returns:
        The URIs written, sorted by relative path. For a `file://` prefix the
        relative paths are percent-encoded, as a `file://` URI requires.

    Raises:
        FileNotFoundError: Before anything is uploaded, if `local_dir` does not
            exist.
        NotADirectoryError: Before anything is uploaded, if `local_dir` is not
            a folder.
        FileNotFoundError: Before anything is uploaded, if an entry under
            `local_dir` is not a file or a link to one (for example a broken
            link).
        OSError: Before anything is uploaded, if a folder under `local_dir`
            cannot be read (for example `PermissionError`).
        ObjectExistsError: With `overwrite=False`, if an object exists at one
            of the URIs. That object is not changed.
    """
    _refuse_options(backend, kwargs)
    root = Path(local_dir)
    if not root.exists():
        raise FileNotFoundError(f"no such folder to upload: {local_dir}")
    if not root.is_dir():
        raise NotADirectoryError(f"not a folder: {local_dir}")

    jobs: list[tuple[str, Path]] = []
    # os.walk skips a folder it cannot read unless onerror raises: the upload
    # would then succeed with files missing.
    def refuse(exc: OSError) -> None:
        raise exc

    for folder, _, names in os.walk(root, onerror=refuse, followlinks=False):
        for name in names:
            path = Path(folder) / name
            if not path.is_file():
                raise FileNotFoundError(f"not a file to upload: {path}")
            jobs.append((path.relative_to(root).as_posix(), path))
    jobs.sort()
    base = uri_prefix.removesuffix("/")
    # A file:// URI is percent-decoded when it is read, so a name such as
    # "%2e%2e%2fx" would point outside the folder. Encode it; keep the "/".
    if urlparse(uri_prefix).scheme.lower() == "file":
        uris = [f"{base}/{quote(relative)}" for relative, _ in jobs]
    else:
        uris = [f"{base}/{relative}" for relative, _ in jobs]

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(upload, uri, path, overwrite=overwrite, backend=backend, **kwargs)
            for uri, (_, path) in zip(uris, jobs)
        ]
        try:
            for future in as_completed(futures):
                future.result()
        except BaseException:
            executor.shutdown(wait=True, cancel_futures=True)
            raise
    printer.message(f"uploaded {len(jobs)} files from {os.fspath(local_dir)} to {uri_prefix}")
    return uris


def list_files(
    uri_prefix: str,
    *,
    recursive: bool = True,
    max_items: int | None = None,
    required_prefix: str | None = None,
    required_ext: str | None = None,
    backend: StorageBackend | None = None,
    **kwargs: Any,
) -> list[str]:
    """List full URIs under a prefix, lexicographically sorted.

    The prefix has folder semantics on every backend: it names the object at
    that exact path (if one exists) plus everything under it as a folder —
    never sibling objects that merely share leading characters
    (`results/run-1` does not match `results/run-10/x`). To match objects by
    partial *filename*, use `required_prefix` instead.

    Args:
        uri_prefix: Prefix to list under (a "directory" URI or an exact
            object URI).
        recursive: If `False`, list only the immediate level.
        max_items: Cap on the number of results, applied after the filename
            filters; `None` means unbounded.
        required_prefix: If given, list only objects whose *filename* starts
            with this (applied to every recursively listed key).
        required_ext: If given, list only objects with this file extension
            (with or without the leading dot).
        backend: A backend to use instead of one built from `uri_prefix` and `kwargs`.
            Build it once and pass it to many calls to reuse its client and
            connections.
        **kwargs: Forwarded to the backend constructor.

    Returns:
        Full URIs (never bare keys), sorted; empty if nothing matches.
    """
    return _resolve(uri_prefix, backend, kwargs).list_files(
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
    backend: StorageBackend | None = None,
    **kwargs: Any,
) -> int:
    """Count objects under a prefix (always recursive) without building URI lists.

    Same folder semantics as `list_files`: the prefix names the object at that
    exact path (if one exists) plus everything under it as a folder, never
    sibling objects that merely share leading characters. The result always
    equals `len(list_files(...))` given the same filters — it just skips
    building the URI list.

    Args:
        uri_prefix: Prefix to count under (a "directory" URI or an exact
            object URI).
        required_prefix: If given, count only objects whose *filename* starts
            with this (applied to every recursively listed key).
        required_ext: If given, count only objects with this file extension
            (with or without the leading dot).
        backend: A backend to use instead of one built from `uri_prefix` and `kwargs`.
            Build it once and pass it to many calls to reuse its client and
            connections.
        **kwargs: Forwarded to the backend constructor.

    Returns:
        The number of matching objects.
    """
    return _resolve(uri_prefix, backend, kwargs).object_count(
        uri_prefix, required_prefix=required_prefix, required_ext=required_ext
    )


def exists(uri: str, *, backend: StorageBackend | None = None, **kwargs: Any) -> bool:
    """Whether `uri` addresses an existing object.

    Args:
        uri: Fully-qualified storage URI.
        backend: A backend to use instead of one built from `uri` and `kwargs`.
            Build it once and pass it to many calls to reuse its client and
            connections.
        **kwargs: Forwarded to the backend constructor.

    Returns:
        `True` if the object exists (directories/prefixes do not count).
    """
    return _resolve(uri, backend, kwargs).exists(uri)


def delete(uri: str, *, backend: StorageBackend | None = None, **kwargs: Any) -> None:
    """Delete the object at `uri`. Idempotent: a missing object is a no-op.

    Args:
        uri: Fully-qualified storage URI.
        backend: A backend to use instead of one built from `uri` and `kwargs`.
            Build it once and pass it to many calls to reuse its client and
            connections.
        **kwargs: Forwarded to the backend constructor.
    """
    _resolve(uri, backend, kwargs).delete(uri)


def extract_flac_header(
    uri: str, *, backend: StorageBackend | None = None, **kwargs: Any
) -> FlacHeader:
    """Parse the STREAMINFO of the FLAC file at `uri`, fetching only header bytes.

    Metadata block bodies (e.g. embedded artwork) are skipped, not downloaded.

    Args:
        uri: Fully-qualified storage URI of a FLAC file.
        backend: A backend to use instead of one built from `uri` and `kwargs`.
            Build it once and pass it to many calls to reuse its client and
            connections.
        **kwargs: Forwarded to the backend constructor.

    Returns:
        The parsed [`FlacHeader`][ondio.types.FlacHeader].

    Raises:
        OndioError: If `uri` is not a valid FLAC stream.
    """
    return _flac.extract_flac_header(_resolve(uri, backend, kwargs), uri)


@overload
def read_flac(
    uri: str,
    start_sec: float | None = None,
    end_sec: float | None = None,
    *,
    decode: Literal[True] = True,
    use_range: bool | None = None,
    backend: StorageBackend | None = None,
    **kwargs: Any,
) -> tuple[np.ndarray, int]: ...


@overload
def read_flac(
    uri: str,
    start_sec: float | None = None,
    end_sec: float | None = None,
    *,
    decode: Literal[False],
    use_range: bool | None = None,
    backend: StorageBackend | None = None,
    **kwargs: Any,
) -> bytes: ...


def read_flac(
    uri: str,
    start_sec: float | None = None,
    end_sec: float | None = None,
    *,
    decode: bool = True,
    use_range: bool | None = None,
    backend: StorageBackend | None = None,
    **kwargs: Any,
) -> tuple[np.ndarray, int] | bytes:
    """Read a FLAC file — whole, or a `[start_sec, end_sec]` window — into memory.

    Windows are cut by sample index: `[round(start_sec * sr),
    round(end_sec * sr))`.

    Args:
        uri: Fully-qualified storage URI of a FLAC file.
        start_sec: Window start in seconds; `None` means from the beginning.
        end_sec: Window end in seconds; `None` means to the end of the stream
            (values past EOF are clamped, like Python slices).
        decode: If `True` (default), return decoded PCM; if `False`, return the
            bytes of a standalone FLAC file — the original bytes exactly when
            no window is given (a plain read), a fresh lossless encode of
            the slice otherwise.
        use_range: Fetch strategy. `None` (default) picks automatically: ranged
            download for files > 50 MB when the window is < 1/3 of the stream,
            full download otherwise. `True`/`False` forces one path.
        backend: A backend to use instead of one built from `uri` and `kwargs`.
            Build it once and pass it to many calls to reuse its client and
            connections.
        **kwargs: Forwarded to the backend constructor.

    Returns:
        With `decode=True`, `(samples, sample_rate)` where `samples` is a
        `float32` array of shape `(frames, channels)` normalized to `[-1, 1]`
        like libsndfile/`soundfile`. With `decode=False`, FLAC bytes.

    Raises:
        ValueError: If the window is empty, negative, starts past EOF, or
            holds no samples.
        OndioError: If any of these is true:

            - `uri` is not a valid FLAC stream.
            - A window was requested and the stream reports zero duration.
            - The audio to decode is corrupt or truncated.
            - A whole-file read meets a stream of unknown length.
            - `decode=False` must re-encode a window of a source that is
              not 8, 16 or 24 bits deep.

    Note:
        The ranged path returns exactly the samples a full download would.
        A byte estimate chooses what to fetch. The frame numbers in the
        fetched bytes give the window's position. If the frames do not
        cover the window, the fetch widens; it fetches each byte at most
        once, so it never fetches more than the file. For a corrupt or
        truncated region the ranged path raises `OndioError` naming
        `use_range=False`. It never starts a full download.
    """
    return _flac.read_flac(
        _resolve(uri, backend, kwargs), uri, start_sec, end_sec,
        decode=decode, use_range=use_range,
    )


def download_flac(
    uri: str,
    out_path: str | os.PathLike[str],
    start_sec: float | None = None,
    end_sec: float | None = None,
    *,
    use_range: bool | None = None,
    backend: StorageBackend | None = None,
    **kwargs: Any,
) -> None:
    """Download a FLAC file — whole, or a `[start_sec, end_sec]` window — to a local path.

    With no window this is a plain byte-for-byte download. With one, the
    window is decoded, cut by sample index, and re-encoded — losslessly
    identical PCM, but not byte-identical to the source. Window and fetch
    semantics, and the ranged-path note, are as in
    [`read_flac`][ondio.dispatcher.read_flac].

    (Counterpart of legacy `soundhub_utils` `io.read_flac(src, dest, ...)`.)

    Args:
        uri: Fully-qualified storage URI of a FLAC file.
        out_path: Local `.flac` destination path; parent directories are created.
        start_sec: Window start in seconds; `None` means from the beginning.
        end_sec: Window end in seconds; `None` means to the end of the stream.
        use_range: As in [`read_flac`][ondio.dispatcher.read_flac].
        backend: A backend to use instead of one built from `uri` and `kwargs`.
            Build it once and pass it to many calls to reuse its client and
            connections.
        **kwargs: Forwarded to the backend constructor.

    Raises:
        ValueError: If the window is empty, negative, starts past EOF, or
            holds no samples.
        OndioError: If any of these is true:

            - `uri` is not a valid FLAC stream.
            - A window was requested and the stream reports zero duration.
            - The window's audio is corrupt or truncated.
            - The source is not 8, 16 or 24 bits deep, so the window cannot
              be re-encoded.
    """
    _flac.download_flac(
        _resolve(uri, backend, kwargs), uri, out_path, start_sec, end_sec,
        use_range=use_range,
    )


