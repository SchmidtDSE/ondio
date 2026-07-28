"""AWS S3 backend."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from urllib.parse import urlparse

from ondio.backends.protocol import _exact_object_matches, _folder_match, _matches_filters
from ondio.types import AuthError, ObjectNotFoundError, OndioError

_NOT_FOUND_CODES = {"404", "NoSuchKey", "NoSuchBucket"}
_AUTH_CODES = {"403", "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken"}


def _split(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"not an s3:// URI: {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


class AwsBackend:
    """StorageBackend for AWS S3 (`s3://bucket/key` URIs), via boto3.

    See [`StorageBackend`][ondio.backends.StorageBackend] for method
    semantics. Credentials resolve through boto3's native chain; botocore
    errors are translated into ondio's exception types.

    Args:
        **session_kwargs: Forwarded to `boto3.Session` (e.g. `profile_name`,
            `region_name`).
    """

    def __init__(self, **session_kwargs):
        import boto3

        self._client = boto3.Session(**session_kwargs).client("s3")

    @contextlib.contextmanager
    def _translate(self, uri: str):
        from botocore.exceptions import ClientError

        try:
            yield
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _NOT_FOUND_CODES:
                raise ObjectNotFoundError(f"no such object: {uri}") from exc
            if code in _AUTH_CODES:
                raise AuthError(f"access denied ({code}): {uri}") from exc
            raise OndioError(f"S3 error ({code}) for {uri}") from exc

    def read(self, uri: str) -> bytes:
        bucket, key = _split(uri)
        with self._translate(uri):
            return self._client.get_object(Bucket=bucket, Key=key)["Body"].read()

    def read_range(self, uri: str, start_byte: int, end_byte: int | None) -> bytes:
        if start_byte < 0:
            raise ValueError(f"start_byte must be >= 0, got {start_byte}")
        if end_byte is not None and end_byte < start_byte:
            raise ValueError(f"end_byte {end_byte} < start_byte {start_byte}")
        bucket, key = _split(uri)
        header = f"bytes={start_byte}-" if end_byte is None else f"bytes={start_byte}-{end_byte}"
        from botocore.exceptions import ClientError

        try:
            return self._client.get_object(Bucket=bucket, Key=key, Range=header)["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "InvalidRange":
                return b""  # start past EOF: protocol says return the available bytes
            with self._translate(uri):
                raise

    def size(self, uri: str) -> int:
        bucket, key = _split(uri)
        with self._translate(uri):
            return self._client.head_object(Bucket=bucket, Key=key)["ContentLength"]

    def write(self, uri: str, data: bytes) -> None:
        bucket, key = _split(uri)
        with self._translate(uri):
            self._client.put_object(Bucket=bucket, Key=key, Body=data)

    def download(self, uri: str, out_path: str | os.PathLike[str]) -> None:
        bucket, key = _split(uri)
        dest = Path(out_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._translate(uri):
            self._client.download_file(bucket, key, str(dest))

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
        exact_uri = f"s3://{bucket}/{exact_key}" if exact_key is not None else None
        # required_prefix filters the *filename* of every (recursively listed)
        # key, so it cannot be folded into the native Prefix — that would miss
        # nested keys like sub/chunk_003.flac.
        # Likewise the native MaxItems counts unfiltered keys, so max_items
        # must be applied after filtering. The native cap survives the
        # exact-key check, though: exact_key is a strict prefix of every key
        # under "exact_key/", so it sorts ahead of them all and the final
        # slice still yields the true first max_items.
        filtered = required_prefix is not None or required_ext is not None
        paginate_kwargs: dict = {"Bucket": bucket, "Prefix": key}
        if not recursive:
            paginate_kwargs["Delimiter"] = "/"
        if max_items is not None and not filtered:
            paginate_kwargs["PaginationConfig"] = {"MaxItems": max_items}

        results = []
        with self._translate(uri_prefix):
            for page in self._client.get_paginator("list_objects_v2").paginate(**paginate_kwargs):
                for obj in page.get("Contents", []):
                    if obj["Key"].endswith("/"):  # skip folder placeholders
                        continue
                    name = obj["Key"].rsplit("/", 1)[-1]
                    if _matches_filters(name, required_prefix, required_ext):
                        results.append(f"s3://{bucket}/{obj['Key']}")
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
        exact_uri = f"s3://{bucket}/{exact_key}" if exact_key is not None else None
        # required_prefix filters the *filename* of every (recursively listed)
        # key, so it cannot be folded into the native Prefix — that would miss
        # nested keys like sub/chunk_003.flac. Folder placeholders are skipped
        # (not counted by list_files, so not counted here either) — hence no
        # KeyCount fast path.
        count = 0
        with self._translate(uri_prefix):
            for page in self._client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=key):
                for obj in page.get("Contents", []):
                    if obj["Key"].endswith("/"):
                        continue
                    name = obj["Key"].rsplit("/", 1)[-1]
                    if _matches_filters(name, required_prefix, required_ext):
                        count += 1
        if exact_uri is not None and _exact_object_matches(
            self.size, exact_uri, required_prefix, required_ext
        ):
            count += 1
        return count

    def exists(self, uri: str) -> bool:
        try:
            self.size(uri)
            return True
        except ObjectNotFoundError:
            return False

    def delete(self, uri: str) -> None:
        bucket, key = _split(uri)
        with self._translate(uri):
            self._client.delete_object(Bucket=bucket, Key=key)  # S3 delete is idempotent
