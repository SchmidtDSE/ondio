# Changelog

## Unreleased

- `ondio.backends` exports `StorageBackend` and the four backend classes
  (`AwsBackend`, `GcsBackend`, `HttpBackend`, `LocalBackend`).
- A documentation site, built with Zensical.

## 0.3.1 — 2026-10-30

- `endpoint_url=` sets the S3 or GCS endpoint for one call, for example an S3
  store that is not AWS. See [Endpoints](guide/objects.md#endpoints).
- `ondio.get_environment(uri, **options)` gives the environment variables that
  make another process's ondio calls use the same options.
- `create` works on GCS, as a conditional upload. GCS transport failures,
  checksum mismatches, and expired credentials raise ondio errors.

## 0.3.0 — 2026-10-30

This release changes FLAC behavior. See
[Migrating from soundhub_utils](guide/differences.md#ranged-windowed-flac-reads)
for the details.

- **Ranged FLAC windows are sample-exact.** Before, a ranged read estimated
  byte offsets from the header's duration, and on long recordings a window could
  come back from the wrong time, by about 30 s, with no error.
- **Damaged input raises `OndioError`** instead of returning short or invented
  audio. A ranged failure never starts a full download on its own.
- **soundfile replaces pydub and ffmpeg.** Nothing needs to be on `PATH`. A
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
