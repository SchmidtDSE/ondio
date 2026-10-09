"""Google Cloud Storage backend."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from urllib.parse import urlparse

from ondio.backends.protocol import _exact_object_matches, _folder_match, _matches_filters
from ondio.types import AuthError, ObjectNotFoundError, OndioError, UnsupportedOperationError


def _split(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "gs" or not parsed.netloc:
        raise ValueError(f"not a gs:// URI: {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


class GcsBackend:
    """StorageBackend for Google Cloud Storage (`gs://bucket/key` URIs).

    See [`StorageBackend`][ondio.backends.StorageBackend] for method
    semantics. Requires the `gcs` extra. Credentials resolve through the
    google-cloud-storage client's native chain; API errors are translated
    into ondio's exception types.

    Args:
        **client_kwargs: Forwarded to `google.cloud.storage.Client`
            (e.g. `project`).
    """

    def __init__(self, **client_kwargs):
        try:
            from google.cloud import storage
        except ImportError as exc:
            raise ImportError(
                "gs:// support requires the 'gcs' extra: pip install ondio[gcs]"
            ) from exc
        self._client = storage.Client(**client_kwargs)

    @contextlib.contextmanager
    def _translate(self, uri: str):
        from google.api_core import exceptions as gexc

        try:
            yield
        except gexc.NotFound as exc:
            raise ObjectNotFoundError(f"no such object: {uri}") from exc
        except (gexc.Forbidden, gexc.Unauthorized) as exc:
            raise AuthError(f"access denied: {uri}") from exc
        except gexc.GoogleAPICallError as exc:
            raise OndioError(f"GCS error for {uri}: {exc}") from exc

    def _blob(self, uri: str):
        bucket, key = _split(uri)
        return self._client.bucket(bucket).blob(key)

    def read(self, uri: str) -> bytes:
        with self._translate(uri):
            return self._blob(uri).download_as_bytes()

    def read_range(self, uri: str, start_byte: int, end_byte: int | None) -> bytes:
        if start_byte < 0:
            raise ValueError(f"start_byte must be >= 0, got {start_byte}")
        if end_byte is not None and end_byte < start_byte:
            raise ValueError(f"end_byte {end_byte} < start_byte {start_byte}")
        from google.api_core import exceptions as gexc

        with self._translate(uri):
            try:
                # google-cloud-storage end is inclusive, matching the protocol
                return self._blob(uri).download_as_bytes(start=start_byte, end=end_byte)
            except gexc.RequestRangeNotSatisfiable:
                # must be caught before _translate sees it: it subclasses
                # GoogleAPICallError and would be swallowed into OndioError
                return b""  # start past EOF: protocol says return the available bytes

    def size(self, uri: str) -> int:
        bucket, key = _split(uri)
        with self._translate(uri):
            blob = self._client.bucket(bucket).get_blob(key)
        if blob is None:
            raise ObjectNotFoundError(f"no such object: {uri}")
        return blob.size

    def write(self, uri: str, data: bytes) -> None:
        with self._translate(uri):
            self._blob(uri).upload_from_string(data, content_type="application/octet-stream")

    def create(self, uri: str, data: bytes) -> None:
        raise UnsupportedOperationError(f"create is not supported on GCS yet: {uri}")

    def download(self, uri: str, out_path: str | os.PathLike[str]) -> None:
        dest = Path(out_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._translate(uri):
            self._blob(uri).download_to_filename(str(dest))

    def upload(self, uri: str, source_path: str | os.PathLike[str]) -> None:
        with self._translate(uri):
            self._blob(uri).upload_from_filename(
                str(source_path), content_type="application/octet-stream"
            )

    def list_files(
        self,
        uri_prefix: str,
        *,
        recursive: bool = True,
        max_items: int | None = None,
        required_prefix: str | None = None,
        required_ext: str | None = None,
    ) -> list[str]:
        bucket, key = _split(uri_prefix)
        key, exact_key = _folder_match(key)
        exact_uri = f"gs://{bucket}/{exact_key}" if exact_key is not None else None
        # The filters apply to the *filename* of every (recursively listed)
        # key, so they cannot be folded into the native prefix, and the native
        # max_results counts unfiltered keys — cap after filtering instead.
        # The native cap survives the exact-key check, though: exact_key is a
        # strict prefix of every key under "exact_key/", so it sorts ahead of
        # them all and the final slice still yields the true first max_items.
        filtered = required_prefix is not None or required_ext is not None
        with self._translate(uri_prefix):
            blobs = self._client.list_blobs(
                bucket,
                prefix=key,
                delimiter=None if recursive else "/",
                max_results=None if filtered else max_items,
            )
            results = [
                f"gs://{bucket}/{blob.name}"
                for blob in blobs
                if not blob.name.endswith("/")
                and _matches_filters(blob.name.rsplit("/", 1)[-1], required_prefix, required_ext)
            ]
        if exact_uri is not None and _exact_object_matches(
            self.size, exact_uri, required_prefix, required_ext
        ):
            results.append(exact_uri)
        results.sort()
        return results if max_items is None else results[:max_items]

    def object_count(
        self,
        uri_prefix: str,
        *,
        required_prefix: str | None = None,
        required_ext: str | None = None,
    ) -> int:
        bucket, key = _split(uri_prefix)
        key, exact_key = _folder_match(key)
        exact_uri = f"gs://{bucket}/{exact_key}" if exact_key is not None else None
        # required_prefix filters the *filename* of every (recursively listed)
        # key, so it cannot be folded into the native prefix — that would miss
        # nested keys like sub/chunk_003.flac. Folder placeholders are skipped,
        # as in list_files.
        count = 0
        with self._translate(uri_prefix):
            for blob in self._client.list_blobs(bucket, prefix=key):
                if blob.name.endswith("/"):
                    continue
                if _matches_filters(blob.name.rsplit("/", 1)[-1], required_prefix, required_ext):
                    count += 1
        if exact_uri is not None and _exact_object_matches(
            self.size, exact_uri, required_prefix, required_ext
        ):
            count += 1
        return count

    def exists(self, uri: str) -> bool:
        with self._translate(uri):
            return self._blob(uri).exists()

    def delete(self, uri: str) -> None:
        from google.api_core import exceptions as gexc

        try:
            with self._translate(uri):
                self._blob(uri).delete()
        except ObjectNotFoundError:
            pass  # idempotent delete, per the protocol
        except gexc.NotFound:  # pragma: no cover - mapped above
            pass
