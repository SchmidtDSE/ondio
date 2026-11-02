# Changelog

## 0.5.0 - 2026-11-02

Everything here is an addition. Code written for 0.4 needs no change.

**New:**

- **`upload(..., overwrite=False)`** writes a file only if no object exists at the
  URI, and raises `ObjectExistsError` otherwise. On S3 it is one conditional `PUT`,
  limited to 5 GiB; a larger file raises `UnsupportedOperationError` before anything
  is sent. See [Files on disk](guide/objects.md#files-on-disk).
- **`upload_files(uri_prefix, local_dir)`** uploads every file under a local folder,
  keeping relative paths, and returns the URIs sorted by relative path. It takes
  `overwrite=` and `max_workers=`.
- **`anonymous=True`** for `s3://` and `gs://` reads a public bucket with no
  credentials, per call or on `AwsBackend` and `GcsBackend`. See
  [Public buckets](guide/objects.md#public-buckets).

**Other changes:**

- **A local `upload` replaces the file in one step.** A reader never sees a partly
  written file. A replaced file keeps its permissions, and a link at the target is
  followed, as before: the file it points to is replaced.
- **Files from a local `create` get the normal permissions**, as `write` gives
  them, rather than being readable only by their owner.
- **`StorageBackend.upload` has an `overwrite` keyword.** `ondio` passes it only
  when it is `False`, so a backend of your own needs it only to support
  `overwrite=False`.

## 0.4.0 - 2026-11-02

This release changes error types and HTTP behavior. Code that catches `OndioError`
needs no change.

**Changes that can break a caller:**

- **SDK errors arrive as `ondio` errors.** Errors from `boto3`, `botocore`,
  `google-auth`, `google-cloud-storage` and `requests` that escaped before (such as
  `NoCredentialsError`, `ProfileNotFound`, `EndpointConnectionError`,
  `DefaultCredentialsError` and `requests.ConnectionError`) now raise `AuthError`,
  `NetworkError` or `OndioError`. The SDK error is the `__cause__`. Backend
  constructors are translated too. A bad URL or HTTP header is still `ValueError`, and a missing
  extra is still `ImportError`. See [Errors](guide/objects.md#errors).
- **S3 and GCS `create` with no credentials raises `AuthError`**, not "could not
  confirm create". A transport failure in `create` raises `NetworkError`.
- **HTTP requests time out.** The default is `(10, 60)` seconds (connect, read).
  `timeout=None` restores no limit.
- **HTTP redirects that are not followed raise `OndioError`.** This applies only
  with the new `allow_redirects=False`.
- **`create` over HTTP is refused by `HttpBackend`**, not before backend setup. So
  without the `http` extra it raises `ImportError`, as `write` does.
- **`padding_ratio` is removed** from `read_flac` and `download_flac`. It was
  ignored since 0.3.0.
- **The `gcs` extra needs `google-cloud-storage` 3 or later.** `create` already
  needed it; now every GCS call does.
- **A failed `download` leaves no partial file**, on every backend. A file already
  at the local path is kept. On GCS, HTTP and local paths, a file that a download
  replaces keeps its permissions.
- **An S3 request that `botocore` refuses before sending it is `ValueError`**, for
  example one with an empty key. Before, `ParamValidationError` escaped.

**New:**

- `ondio.NetworkError`: no answer came from the store. The request can be retried.
- `HttpBackend` options: `headers`, `auth`, `cookies`, `verify`, `timeout` and
  `allow_redirects`. They also work per call, as in `ondio.read(url, headers=...)`.
  A redirect to another host gets none of the cookies or headers. See
  [Private HTTPS files](guide/objects.md#private-https-files).
- `AwsBackend(client=...)` and `GcsBackend(client=...)` use a client that you made,
  for example an unsigned S3 client for a public bucket.
- Every public function takes `backend=`, a backend to use instead of one built from
  the URI. See [Reusing a backend](guide/objects.md#reusing-a-backend).
- `ondio.read_range(uri, start_byte, end_byte)` reads an inclusive byte range.
- `ondio.backends` exports `StorageBackend` and the four backend classes
  (`AwsBackend`, `GcsBackend`, `HttpBackend`, `LocalBackend`).
- A documentation site, built with Zensical.

## 0.3.1 — 2026-10-30

- `endpoint_url=` sets the S3 or GCS endpoint for one call, for example an S3
  store that is not AWS. See [Endpoints](guide/objects.md#endpoints).
- `ondio.get_environment(uri, **options)` gives the environment variables that
  make another process's `ondio` calls use the same options.
- `create` works on GCS, as a conditional upload. GCS transport failures,
  checksum mismatches, and expired credentials raise `ondio` errors.

## 0.3.0 — 2026-10-30

This release changes FLAC behavior. See
[Migrating from `soundhub_utils`](guide/differences.md#ranged-windowed-flac-reads)
for the details.

- **Ranged FLAC windows are sample-exact.** Before, a ranged read estimated
  byte offsets from the header's duration, and on long recordings a window could
  come back from the wrong time, by about 30 s, with no error.
- **Damaged input raises `OndioError`** instead of returning short or invented
  audio. A ranged failure never starts a full download on its own.
- **`soundfile` replaces pydub and ffmpeg.** Nothing needs to be on `PATH`. A
  windowed re-encode of a 12- or 20-bit source raises `OndioError`, because
  libsndfile writes only 8, 16, and 24 bits.
- `padding_ratio` is deprecated and ignored.

## 0.2.4 — 2026-10-13

- URIs that would read as a different path are refused with `ValueError`: a
  `file:`, `s3://`, or `gs://` URI with `?` or `#`, whose text after it would be
  dropped, and a `file:` URI that does not start with `file://`.

## 0.2.3 — 2026-10-09

- First release published from CI. `create` and `locate`, added in 0.2.2,
  reach PyPI with it.
- 0.2.2 was tagged but not published.

## 0.2.2 — 2026-10-09 (not published)

- `ondio.create` writes an object only if none exists.
- `ondio.locate` gives the store and path a URI names, without touching storage.

## 0.2.1 — 2026-10-05

- `ondio.upload` streams a file from disk instead of reading it into memory.

## 0.2.0 — 2026-07-28

- Listing prefixes have folder semantics on every backend:
  `results/run-1` matches `results/run-1/…` but not `results/run-10/…`.
- `read_json` replaces `download_json`, to follow the read/write and
  download/upload naming rule.

## 0.1.x — 2026-07

- First releases: URI-first IO over S3, GCS, HTTP, and local disk, with FLAC
  and Parquet helpers.
