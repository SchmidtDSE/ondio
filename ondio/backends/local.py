"""Local-filesystem backend."""

from __future__ import annotations

import os
import shutil 

from pathlib import Path
from urllib.parse import unquote, urlparse

from ondio.backends.protocol import _matches_filters
from ondio.types import AuthError, ObjectNotFoundError

def _to_path(uri: str) -> Path:
    if uri.startswith("file://"):
        parsed = urlparse(uri)
        if parsed.netloc not in ("", "localhost"):
            raise ValueError(f"file:// URIs with a remote host are not supported: {uri!r}")
        return Path(unquote(parsed.path))
    return Path(uri)

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


    def download(self, uri: str, out_path: str | os.PathLike[str]) -> None:
        src = _to_path(uri)
        if not src.is_file():
            raise ObjectNotFoundError(f"no such object: {uri}")
        dest = Path(out_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)

    def upload(self, uri: str, source_path: str | os.PathLike[str]) -> None:
        path = _to_path(uri)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, path)
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
        as_file_uri = uri_prefix.startswith("file://")
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
