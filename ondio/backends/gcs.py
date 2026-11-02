"""Google Cloud Storage backend."""

from __future__ import annotations

import contextlib
import os
from urllib.parse import urlparse

from ondio.backends.protocol import (
    _exact_object_matches,
    _folder_match,
    _matches_filters,
    _replace_on_success,
)
from ondio.types import AuthError, NetworkError, ObjectExistsError, ObjectNotFoundError, OndioError


def _split(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "gs" or not parsed.netloc:
        raise ValueError(f"not a gs:// URI: {uri!r}")
    # urlparse would drop the text after ? or #, so the key would not be the one given.
    if "?" in uri or "#" in uri:
        raise ValueError(f"gs:// URIs with a query or fragment are not supported: {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def _transport_errors() -> tuple[type[Exception], ...]:
    """The SDK errors that mean no answer came from GCS."""
    import requests
    from google.api_core import exceptions as gexc
    from google.auth.exceptions import TransportError as GoogleTransportError

    return (
        GoogleTransportError,
        requests.ConnectionError,
        requests.Timeout,
        requests.exceptions.ChunkedEncodingError,  # the body stopped in the middle
        gexc.RetryError,
    )


class GcsBackend:
    """`StorageBackend` for Google Cloud Storage (`gs://bucket/key` URIs).

    See [`StorageBackend`][ondio.backends.StorageBackend] for method
    semantics. Requires the `gcs` extra. Credentials resolve through the
    `google-cloud-storage` client's native chain; API errors are translated
    into `ondio`'s exception types: credential errors are `AuthError`, and
    connection failures and timeouts are `NetworkError`.

    Args:
        endpoint_url: The GCS API endpoint. The client still uses its normal
            credentials. `None` uses the client's own choice.
        client: A `google.cloud.storage.Client` to use as given. No other
            option can be given with it.
        **client_kwargs: Forwarded to `google.cloud.storage.Client`
            (e.g. `project`).

    Raises:
        ValueError: If both `endpoint_url` and `client_options` are given, or
            `client` with another option.
    """

    def __init__(self, endpoint_url: str | None = None, *, client=None, **client_kwargs):
        try:
            from google.cloud import storage
        except ImportError as exc:
            raise ImportError(
                "gs:// support requires the 'gcs' extra: pip install ondio[gcs]"
            ) from exc
        if client is not None:
            options = sorted([*client_kwargs, *(["endpoint_url"] if endpoint_url is not None else [])])
            if options:
                raise ValueError(f"give client or {', '.join(options)}, not both")
            self._client = client
            return
        if endpoint_url is not None:
            # If both are given, one could silently override the other.
            if "client_options" in client_kwargs:
                raise ValueError("give endpoint_url or client_options, not both")
            client_kwargs["client_options"] = {"api_endpoint": endpoint_url}
        with self._translate("GCS client setup"):
            self._client = storage.Client(**client_kwargs)

    @contextlib.contextmanager
    def _translate(self, uri: str):
        import requests
        from google.api_core import exceptions as gexc
        from google.auth.exceptions import DefaultCredentialsError, RefreshError
        from google.cloud.storage.exceptions import DataCorruption, InvalidResponse

        try:
            yield
        except gexc.NotFound as exc:
            raise ObjectNotFoundError(f"no such object: {uri}") from exc
        except (gexc.Forbidden, gexc.Unauthorized) as exc:
            raise AuthError(f"access denied: {uri}") from exc
        except gexc.GoogleAPICallError as exc:
            raise OndioError(f"GCS error for {uri}: {exc}") from exc
        # These messages name only the type: the SDK's text can hold a request URL,
        # and an upload URL carries an upload_id that grants write access.
        except (DefaultCredentialsError, RefreshError) as exc:
            raise AuthError(f"GCS credentials failed for {uri}: {type(exc).__name__}") from exc
        except _transport_errors() as exc:
            # Includes RetryError: a GoogleAPIError that the GoogleAPICallError clause misses.
            raise NetworkError(f"GCS request failed for {uri}: {type(exc).__name__}") from exc
        except (InvalidResponse, DataCorruption, requests.RequestException) as exc:
            raise OndioError(f"GCS error for {uri}: {type(exc).__name__}") from exc

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
        # A conditional upload: GCS refuses it with 412 if the object exists.
        import requests
        from google.api_core import exceptions as gexc
        from google.auth.exceptions import RefreshError
        from google.cloud.storage.exceptions import DataCorruption

        with self._translate(uri):
            try:
                self._blob(uri).upload_from_string(
                    data, content_type="application/octet-stream", if_generation_match=0
                )
            except gexc.PreconditionFailed as exc:
                # must be caught before _translate sees it: it subclasses
                # GoogleAPICallError and would become a plain OndioError
                raise ObjectExistsError(f"object already exists: {uri}") from exc
            except RefreshError as exc:
                raise AuthError(f"could not refresh GCS credentials: {uri}") from exc
            except _transport_errors() as exc:
                # The request may have reached GCS even though no response arrived.
                raise NetworkError(
                    f"could not confirm create for {uri}: {type(exc).__name__}"
                ) from exc
            except requests.exceptions.RequestException as exc:
                raise OndioError(
                    f"could not confirm create for {uri}: {type(exc).__name__}"
                ) from exc
            except DataCorruption as exc:
                # The client tries to delete the object before it raises this.
                raise OndioError(f"GCS checksum mismatch for {uri}") from exc

    def download(self, uri: str, out_path: str | os.PathLike[str]) -> None:
        # The client deletes its file only for some errors, and opens it with "wb".
        with _replace_on_success(out_path) as temporary, self._translate(uri):
            self._blob(uri).download_to_filename(str(temporary))

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
