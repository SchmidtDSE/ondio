# ondio

`ondio` --- _ondas_ (Spanish for waves) + io --- is a Python package for uniform IO of
audio data (specifically just `.flac` at the moment) and results derived from
bioacoustic models (specifically `.json` and `.parquet` files) across a variety of
sources: AWS S3, GCS, HTTP/HTTPS, and local filesystems.

Every public function takes a URI; the platform is inferred from the URI scheme and
the call is dispatched to the matching backend, so the same code runs unchanged
against an S3 bucket, a GCS bucket, a web server, or a local directory.

`ondio` is a spin-off of [`soundhub_utils`](https://github.com/SchmidtDSE/soundhub_utils), with just the lightweight components to handle IO from generic backends.

## Naming: `read`/`write` vs `download`/`upload`

The API draws one distinction consistently:

- **`read` / `write`** move bytes **in and out of memory**. `read(uri)` returns
  `bytes`; `write(uri, data)` takes them. Nothing touches the local filesystem.
- **`download` / `upload`** move **files on disk**. `download(uri, out_path)`
  streams the object to a local path (creating parent directories);
  `upload(uri, source_path)` pushes a local file to the URI. The URI always
  comes first, the local path second.

The format helpers follow the same rule: `read_flac` returns decoded samples or
FLAC bytes in memory, while `download_flac` writes a local `.flac` file;
`write_json` serializes a Python object straight to a URI, and `download_json`
fetches and parses one.

## Objects, files, and JSON

The same calls work with `s3://`, `gs://`, `http(s)://`, `file://`, or plain
local paths:

```python
import ondio

# moving objects into/from memory
data = ondio.read("s3://my-bucket/audio/rec.flac")        # -> bytes
ondio.write("s3://my-bucket/results/example.txt", b"done")

# JSON <-> Python objects, no local file involved
ondio.write_json("s3://my-bucket/results//summary.json", {"detections": 118})
summary = ondio.download_json("s3://my-bucket/results/summary.json")

# moving files on/off disk
ondio.download("gs://my-bucket/audio/rec.flac", "data/rec.flac")
ondio.upload("s3://my-bucket/audio/rec.flac", "data/rec.flac")

# many objects into one directory, named by basename; concurrent downloads,
# returned local paths follow the input order
local = ondio.download_files(uris, "results/", max_workers=8)

# listing and management — filename filters compose with max_items
uris = ondio.list_files("s3://my-bucket/results/")
flacs = ondio.list_files("s3://my-bucket/audio/", required_ext="flac")
chunks = ondio.list_files("s3://my-bucket/audio/", required_prefix="chunk_", max_items=100)
n_flacs = ondio.object_count("s3://my-bucket/audio/", required_ext="flac")
if ondio.exists("s3://my-bucket/tmp/scratch.json"):
    ondio.delete("s3://my-bucket/tmp/scratch.json")

# keyword arguments are forwarded to the backend constructor
# (for s3:// that's boto3.Session — e.g. picking an AWS profile)
ondio.list_files("s3://my-bucket/audio/", profile_name="dse")
```

## FLAC

FLAC-specific IO lives behind the same read/download split. Decoding requires
the ffmpeg CLI on PATH (pydub and numpy are core dependencies); header parsing
and the whole-file `decode=False` path don't touch ffmpeg.

```python
import ondio

# parse STREAMINFO fetching only header bytes — never the audio
header = ondio.extract_flac_header("s3://my-bucket/audio/rec.flac")
header.sample_rate, header.channels, header.duration

# in memory: decode a 3 s window to PCM — a float32 array of shape
# (frames, channels) in [-1, 1] plus the sample rate, like libsndfile
samples, rate = ondio.read_flac("s3://my-bucket/audio/rec.flac", 60.0, 63.0)

# in memory: the same window as bytes of a standalone .flac (lossless re-encode)
clip = ondio.read_flac("s3://my-bucket/audio/rec.flac", 60.0, 63.0, decode=False)

# in memory: whole file with decode=False is the original bytes exactly, no ffmpeg
raw = ondio.read_flac("s3://my-bucket/audio/rec.flac", decode=False)

# on disk: cut a clip straight to a local .flac file
ondio.download_flac("s3://my-bucket/audio/rec.flac", "clips/rec_60-63.flac", 60.0, 63.0)

# on disk: no window means a plain byte-for-byte download
ondio.download_flac("s3://my-bucket/audio/rec.flac", "data/rec.flac")
```

### Windowed reads of remote files

Windowed reads of large remote files fetch only an estimated byte range around
the window instead of the whole object. Using byte ranges path is chosen automatically
for files over 50 MB when the window is under a third of the stream duration;
`use_range=True`/`False` forces either path. `padding_ratio` (default 0.25)
controls how much extra audio is fetched on each side of the window to absorb
byte-estimate error.

The byte estimate positions the window using the header's duration, so ranged
slices are aligned only approximately (within tens of milliseconds) — but they are
never silently wrong in length: a ranged fetch that fails to decode or comes
back too short to cover the window raises `OndioError` naming `use_range=False`
as the exact fallback, rather than returning short or fabricated audio. A
header that lies about the duration (truncated recorder files exist) can still
place a covering window at the wrong position without an error — pass
`use_range=False` whenever the header cannot be trusted; the full download
reads the window exactly.

## Parquet

Model results go out as hive-partitioned parquet datasets via
`ondio.parquet.upload_parquet` (requires the `parquet` extra for pyarrow). The
dataset is written to a local temp directory with pyarrow, then uploaded
file-by-file through the backend — pyarrow's native S3/GCS filesystems are
deliberately not used (this would require extra credential config), so credentials 
follow the same path as every other `ondio` call.

```python
import pyarrow as pa
from ondio.parquet import upload_parquet

table = pa.table({
    "site": ["A", "A", "B"],
    "date": ["2026-04-02", "2026-04-02", "2026-04-03"],
    "species": ["GRSP", "WEME", "GRSP"],
    "confidence": [0.91, 0.87, 0.66],
})

# writes s3://my-bucket/results/run-1/site=A/date=2026-04-02/part-0.parquet, ...
upload_parquet("s3://my-bucket/results/run-1", table, partition_cols=["site", "date"])

# partition_cols=[] writes a flat (unpartitioned) dataset under the prefix
upload_parquet("s3://my-bucket/results/run-2/", table, partition_cols=[])
```

Details of use:

- **`overwrite`** — by default (`overwrite=False`) the call refuses with
  `OndioError` if any objects already exist under the prefix, so two runs can
  never silently merge into one dataset. `overwrite=True` deletes everything
  under the prefix first, then writes.
- **`compression`** — parquet codec, default `"snappy"`.
- **`max_workers`** — the per-file uploads run in a thread pool; this caps its
  size (default lets the executor decide).
- Backend constructor kwargs pass through as everywhere else
  (e.g. `profile_name="fieldwork"` for S3).

There is no `download_parquet`: readers should point their query engine
(pyarrow, DuckDB, polars) at the dataset directly, or fetch individual files
with `ondio.download` / `ondio.list_files`.

## Known differences from soundhub_utils

`ondio` is not a drop-in port; the differences below are deliberate. The FLAC
ones were established empirically by running both implementations side by side
on synthetic fixtures and on real field recordings.

### API shape

- One URI-first dispatcher replaces the per-platform modules (`io.aws`,
  `io.gcs`, `io.url`): the URI scheme picks the backend, and platform SDKs and
  their types never leak through the public API.
- The `read`/`write` (memory) vs `download`/`upload` (disk) naming rule is
  applied consistently. Legacy's `io.read_flac(src, dest, ...)` actually wrote
  a file to disk; its counterpart here is `download_flac(uri, out_path, ...)`.
- Failures raise typed exceptions (`OndioError`, `ObjectNotFoundError`,
  `AuthError`, ...) instead of only being logged: logging (via cocina's
  `Printer`, as in soundhub_utils) narrates progress and decisions, but is
  never the sole signal that something went wrong.
- `download_files` raises `ValueError` upfront when two URIs share a basename
  (they would overwrite each other in the destination directory); legacy
  silently clobbered. It also downloads concurrently and dispatches each URI
  independently, so schemes may be mixed in one call.
- There is no per-call `printer=` argument (call kwargs are reserved for
  backend configuration). Narration goes to cocina's process-wide `Printer`
  singleton when cocina is installed, and is a silent no-op otherwise. To
  route it to your own printer-like object instead (anything with
  `.message`, `.error`, ...), call `ondio.set_printer(obj)` once at startup;
  `ondio.set_printer(None)` restores the default.

### Ranged (windowed) FLAC reads

- **Ranged blobs are decoded behind a reconstructed header.** `ondio` prepends
  a fresh `fLaC` marker plus the file's own STREAMINFO to every fetched byte
  range before handing it to ffmpeg. Legacy passed the bare mid-stream blob,
  leaving ffmpeg to blind-scan for a frame sync: measured on clean files,
  every legacy ranged read came back silently misaligned by 1–18 ms, and
  24-bit blobs could be probed as a video stream and crash the decoder.
- **Undershoots are loud.** A ranged fetch that fails to decode, or comes back
  too short to cover the requested window, raises `OndioError` naming
  `use_range=False` as the exact fallback — there is no silent short result
  and no hidden retry or implicit full download. Legacy's only length check
  was a log message with a 1-second tolerance: it silently returned short
  audio, and on files whose header overstates the duration (truncated recorder
  files exist) it returned fabricated audio for windows past the real end of
  the stream and wrong-position audio for valid windows.
- **Ranged fetches are optional and bounded.** Legacy always byte-ranged
  partial requests, with no full-file fallback. `ondio` ranges automatically
  only for files over 50 MB with windows under a third of the duration, and
  `use_range=False` always forces the exact full-download path.
- **The byte estimate skips the metadata.** Byte positions are interpolated
  from the first audio byte onward, not from byte 0, so header and artwork
  bytes no longer skew the time→byte map.
- **Millisecond offsets are rounded, not truncated.** Float subtraction
  artifacts (`1.4 - 0.4 == 0.9999…`) shaved a millisecond — tens of samples —
  off legacy slices.

One caveat is shared with legacy: a ranged window that decodes and covers the
requested length, but was positioned using a header that lies about the
duration, can still come from the wrong part of the stream with no error —
only undershoots are caught. Pass `use_range=False` whenever the header cannot
be trusted.

### FLAC header parsing

- Legacy's `extract_header` mis-parses the STREAMINFO bit fields: stereo files
  report 1 channel, and 16- and 24-bit files both report 17 bits per sample.
  `ondio.extract_flac_header` parses the block per the FLAC spec (verified
  against libsndfile) and fetches only header bytes, skipping metadata block
  bodies such as embedded artwork instead of downloading them.

## Architecture

`ondio` defines a unified interface for reading/writing files across different platforms, where the target platform is inferred based on the structure of the URI. The URI structure is used to dispatch to platform specific implementations, combining the [strategy](https://en.wikipedia.org/wiki/Strategy_pattern) and [registry](https://martinfowler.com/eaaCatalog/registry.html) design patterns.

### Layers

`ondio` follows a layered structure. The primary layer users interact with is the
public API: generic read/write methods plus format-specific ones.

The second layer is the registry, which maps URI schemes to approriate backends, and
specific helpers for `.flac` and `.parquet` files. File-format specific logic only lives at
this layer --- the backends only operate on bytes and arbitrary files on disk, and never
implement format-specific logic.


| Function | Description |
|-|-|
| `read(uri)` | Read the full object at `uri` into memory as `bytes` |
| `write(uri, data)` | Write in-memory `bytes` to `uri` |
| `write_json(uri, obj)` / `download_json(uri)` | JSON objects, serialized/parsed |
| `download(uri, out_path)` | Download the object at `uri` to a local file |
| `download_files(uris, local_dir)` | Download many objects into a directory, named by basename; returns local paths in input order |
| `upload(uri, source_path)` | Upload a local file to `uri` |
| `extract_flac_header(uri)` | Parse FLAC STREAMINFO, fetching only header bytes |
| `read_flac(uri, start_sec, end_sec, decode=…)` | FLAC (whole or windowed) → PCM array or FLAC bytes |
| `download_flac(uri, out_path, start_sec, end_sec)` | FLAC (whole or windowed) → local `.flac` file |
| `upload_parquet(uri, table, partition_cols)` | `pyarrow.Table` → hive-partitioned dataset (in `ondio.parquet`) |
| `list_files(uri_prefix)` | Full URIs under a prefix, sorted; optional filename prefix/extension filters |
| `object_count(uri_prefix)` | Count objects under a prefix without listing them; same filters |
| `exists(uri)` | Whether the object exists |
| `delete(uri)` | Delete the object; missing objects are a no-op |

Each call resolves a backend from the URI scheme and delegates to it:

```
                              caller
         "s3://…"   "gs://…"   "https://…"   "file://…"   "/path"
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│ dispatcher — the public, URI-first API                          │
│                                                                 │
│   bytes/objects:  read · write · download · upload              │
│                   download_files · exists · delete              │
│                   list_files · object_count                     │
│   json/parquet:   write_json · download_json · upload_parquet   │
│   flac:           extract_flac_header · read_flac               │
│                   download_flac                                 │
└──────────────┬───────────────────────────────┬──────────────────┘
               │ get_backend(uri)              │ passes (backend, uri)
               ▼                               ▼
┌──────────────────────────┐    ┌─────────────────────────────────┐
│ registry                 │    │ format helpers                  │
│                          │    │                                 │
│   URI scheme → platform  │    │   flac.py     header parsing +  │
│   → backend factory      │    │               partial reads     │
│                          │    │                                 │
│                          │    │   parquet.py  upload_parquet    │
│                          │    │               (pyarrow)         │
└──────────────┬───────────┘    └───────────────┬─────────────────┘
               │                    backend-agnostic: built on the protocol
               │                                │
               │                                │
               ▼                                ▼
┌─────────────────────────────────────────────────────────────────┐
│ backends — the StorageBackend protocol                          │
│                                                                 │
│   read · read_range · size · write · download · list_files      │
│   object_count · exists · delete                                │
│                                                                 │
│   ┌─────────┐   ┌───────────┐   ┌───────────┐   ┌─────────────┐ │
│   │ local   │   │ aws (s3:) │   │ gcs (gs:) │   │ url (http:) │ │
│   └────┬────┘   └─────┬─────┘   └─────┬─────┘   └──────┬──────┘ │
└────────┼──────────────┼───────────────┼────────────────┼────────┘
         ▼              ▼               ▼                ▼
     filesystem       boto3     google-cloud-storage  requests
```

- **dispatcher** — what callers import: every function takes a URI, infers the platform, and delegates. No platform types leak out.
- **registry** — the strategy lookup: maps the URI scheme to a platform name and lazily constructs that platform's backend.
- **format helpers** — FLAC and Parquet logic written once against the backend protocol, so partial FLAC reads work identically on S3, GCS, HTTP, and local files. FLAC decoding/slicing shells out to ffmpeg via pydub (requires the ffmpeg CLI on PATH).
- **backends** — one class per platform implementing the byte-level protocol (`read_range` is what makes partial reads cheap on remote files); each wraps its platform SDK and translates its errors to ondio's exception types.
