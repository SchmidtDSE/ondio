"""Local-filesystem backend."""

from __future__ import annotations

import os
import shutil
import uuid

from pathlib import Path
from urllib.parse import unquote, urlparse

from ondio.backends.protocol import _matches_filters, _replace_on_success
from ondio.types import AuthError, ObjectExistsError, ObjectNotFoundError, OndioError

def _is_file_uri(uri: str) -> bool:
    return urlparse(uri).scheme.lower() == "file"


def _to_path(uri: str) -> Path:
    if not _is_file_uri(uri):
        return Path(uri)
    # The forms below would be read as a different path: urlparse drops a query or
    # fragment, and without `//` the rest of the URI is a relative path.
    if not uri.lower().startswith("file://"):
        raise ValueError(f"file: URIs must start with file://: {uri!r}")
    if "?" in uri or "#" in uri:
        raise ValueError(f"file:// URIs with a query or fragment are not supported: {uri!r}")
    parsed = urlparse(uri)
    if parsed.netloc not in ("", "localhost"):
        raise ValueError(f"file:// URIs with a remote host are not supported: {uri!r}")
    return Path(unquote(parsed.path))

def _wrap_oserror(uri: str, exc: OSError) -> Exception:
    if isinstance(exc, PermissionError):
        return AuthError(f"permission denied: {uri}")
    return ObjectNotFoundError(f"no such object: {uri}")


class LocalBackend:
    def read(self, uri: str) -> bytes:
        try:
            return _to_path(uri).read_bytes()
        except OSError as exc:
            raise _wrap_oserror(uri, exc)


    def read_range(self, uri: str, start_byte: int, end_byte: int | None) -> bytes:
        if start_byte < 0:
            raise ValueError(f"start_byte must be >= 0, got {start_byte}")
        if end_byte is not None and end_byte < start_byte:
            raise ValueError(f"end_byte ({end_byte}) < start_byte {start_byte}")

        try:
            with open(_to_path(uri), "rb") as f:
                f.seek(start_byte)
                if end_byte is None:
                    return f.read()
                return f.read(end_byte - start_byte + 1)
        except OSError as exc:
            raise _wrap_oserror(uri, exc)

    def size(self, uri: str) -> int:
        path = _to_path(uri)
        try:
            if not path.is_file():
                raise ObjectNotFoundError(f"no such object: {uri}")
            return path.stat().st_size
        except OSError as exc:
            raise _wrap_oserror(uri, exc)

    def write(self, uri: str, data : bytes) -> None:
        path = _to_path(uri)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as exc:
            raise _wrap_oserror(uri, exc)

    def create(self, uri: str, data: bytes) -> None:
        self._place_new(_to_path(uri), uri, lambda stream: stream.write(data))

    def _place_new(self, path: Path, uri: str, fill) -> None:
        # `fill(stream)` writes the content to a temporary file beside the target.
        # os.link then puts it in place in one step, and fails if the target
        # exists, so no reader sees a partial file and no existing file is replaced.
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
            # "xb" rather than mkstemp: the new file gets the normal permissions.
            try:
                with open(temporary, "xb") as stream:
                    fill(stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError as exc:
                    raise ObjectExistsError(f"object already exists: {uri}") from exc
            finally:
                temporary.unlink(missing_ok=True)
        except PermissionError as exc:
            raise AuthError(f"permission denied: {uri}") from exc
        except OSError as exc:
            raise OndioError(f"could not create {uri}: {exc}") from exc

    def download(self, uri: str, out_path: str | os.PathLike[str]) -> None:
        src = _to_path(uri)
        if not src.is_file():
            raise ObjectNotFoundError(f"no such object: {uri}")
        with _replace_on_success(out_path) as temporary:
            shutil.copyfile(src, temporary)

    def upload(
        self, uri: str, source_path: str | os.PathLike[str], *, overwrite: bool = True
    ) -> None:
        path = _to_path(uri)
        if not overwrite:
            def copy(stream):
                with open(source_path, "rb") as source:
                    shutil.copyfileobj(source, stream)

            self._place_new(path, uri, copy)
            return
        # The copy goes to a temporary file that replaces the target in one step,
        # so a reader never sees a partly written file. A link is followed, as
        # copying onto it would: its target is replaced, not the link.
        if path.is_symlink():
            path = Path(os.path.realpath(path))
        try:
            with _replace_on_success(path) as temporary:
                shutil.copyfile(source_path, temporary)
        except OSError as exc:
            raise _wrap_oserror(uri, exc)

    def list_files(
        self,
        uri_prefix: str,
        *,
        recursive: bool = True,
        max_items: int | None = None,
        required_prefix: str | None = None,
        required_ext: str | None = None,
    ) -> list[str]:
        as_file_uri = _is_file_uri(uri_prefix)
        matches = (
            p
            for p in self._iter_matches(uri_prefix, recursive=recursive)
            if _matches_filters(p.name, required_prefix, required_ext)
        )
        results = sorted(p.as_uri() if as_file_uri else str(p) for p in matches)
        return results if max_items is None else results[:max_items]


    def object_count(
        self,
        uri_prefix: str,
        *,
        required_prefix: str | None = None,
        required_ext: str | None = None,
    ) -> int:
         return sum(
            1
            for path in self._iter_matches(uri_prefix, recursive=True)
            if _matches_filters(path.name, required_prefix, required_ext)
        )

    def exists(self, uri: str) -> bool:
        return _to_path(uri).is_file()

    def delete(self, uri: str) -> None:
        try:
            _to_path(uri).unlink(missing_ok=True)
        except OSError as exc:
            raise _wrap_oserror(uri, exc) from exc

    def _iter_matches(self, uri_prefix: str, *, recursive: bool):
        """Files named by a folder path: an existing directory yields the files
        under it (recursively, or one level deep); a path that is itself a file
        yields just that file; anything else yields nothing. A partial name
        never matches: /data/chunk_ does not match /data/chunk_001.flac."""
        base = _to_path(uri_prefix)
        # A trailing slash names folder contents only, so it excludes the file
        # at that exact path — matching _folder_match on the cloud backends.
        # The check is on the raw URI: _to_path has already dropped the slash.
        if not uri_prefix.endswith("/") and base.is_file():
            yield base
            return
        if not base.is_dir():
            return
        if recursive:
            candidates = (
                Path(dirpath) / name
                for dirpath, _, filenames in os.walk(base)
                for name in filenames
            )
        else:
            candidates = (p for p in base.iterdir() if p.is_file())
        yield from candidates
