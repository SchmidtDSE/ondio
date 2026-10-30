# Migrating from `soundhub_utils`

`ondio` is not a drop-in port. This table maps each `soundhub_utils.io` call to
its replacement; the sections after it explain the differences, which are
deliberate. The FLAC ones were established empirically by running both
implementations side by side on synthetic fixtures and on real field recordings.

## Calls

| `soundhub_utils` | `ondio` |
| --- | --- |
| `io.read(src)` (a platform response object) | `ondio.read(uri)` (`bytes`) |
| `io.read(src, dest)` | `ondio.download(uri, out_path)` |
| `io.read(src, start_byte=…, end_byte=…)` | No public equivalent |
| `io.read_flac(src, dest)` | `ondio.download_flac(uri, out_path)` |
| `io.read_flac(src, dest, start_time=…, end_time=…)` | `ondio.download_flac(uri, out_path, start_sec, end_sec)` |
| `io.read_partial_flac(src, dest, start_time, end_time)` | `ondio.download_flac(uri, out_path, start_sec, end_sec)` |
| `duration=` | Pass `end_sec=start_sec + duration` |
| A FLAC window in memory | `ondio.read_flac(uri, start_sec, end_sec)` (new) |
| `io.extract_flac_header(src)` (a `dict`) | `ondio.extract_flac_header(uri)` (a `FlacHeader`) |
| `platform_name=` | None: the URI scheme picks the backend |
| `client=` | Backend options, such as `profile_name=` (see [Backend options](objects.md#backend-options)) |
| `create_parents=`, `overwrite=` | None: `download` always creates parent directories and replaces an existing file |
| `printer=` | `ondio.set_printer(obj)`, once per process (see [Logging](logging.md)) |

## API shape

- One URI-first dispatcher replaces the per-platform modules (`io.aws`,
  `io.gcs`, `io.url`): the URI scheme picks the backend, and platform SDKs and
  their types never leak through the public API.
- The `read`/`write` (memory) vs `download`/`upload` (disk) naming rule is
  applied consistently. Legacy's `io.read_flac(src, dest, ...)` actually wrote
  a file to disk; its counterpart here is `download_flac(uri, out_path, ...)`.
- Failures raise typed exceptions (`OndioError`, `ObjectNotFoundError`,
  `AuthError`, ...) instead of only being logged: logging (via `cocina`'s
  `Printer`, as in `soundhub_utils`) narrates progress and decisions, but is
  never the sole signal that something went wrong.
- `download_files` raises `ValueError` upfront when two URIs share a basename
  (they would overwrite each other in the destination directory); legacy
  silently clobbered. It also downloads concurrently and dispatches each URI
  independently, so schemes may be mixed in one call.
- There is no per-call `printer=` argument (call kwargs are reserved for
  backend configuration). Narration goes to `cocina`'s process-wide `Printer`
  singleton when `cocina` is installed, and is a silent no-op otherwise. To
  route it to your own printer-like object instead (anything with
  `.message`, `.error`, ...), call `ondio.set_printer(obj)` once at startup;
  `ondio.set_printer(None)` restores the default.

## Ranged (windowed) FLAC reads

- **Ranged windows are sample-exact.** Legacy (and `ondio` before 0.3.0)
  estimated byte offsets linearly from the header's duration and assumed the
  decoded audio began at the estimated time. FLAC's bitrate varies with the
  content, so on real multi-hour recordings windows came back from the wrong
  time — by a median of about 30 s — with no error. `ondio` now locates the
  window from the frame numbers in the fetched bytes.
- **Damaged input raises an error.** A window that can't be located or decoded raises
  `OndioError` naming `use_range=False`; there is no silent short result and
  no implicit full download. Legacy's only length check was a log message with
  a 1-second tolerance: it silently returned short audio, and on files whose
  header overstates the duration it returned fabricated audio for windows past
  the real end of the stream.
- **Ranged fetches are optional and bounded.** Legacy always byte-ranged
  partial requests, with no full-file fallback. `ondio` ranges automatically
  only for files over 50 MB with windows under a third of the duration. A
  ranged read fetches each byte at most once, so never more than the file, and
  `use_range=False` always forces the full-download path.
- **Windows are cut by sample, not millisecond.** Legacy sliced at integer
  milliseconds (truncating, so float artifacts like `1.4 - 0.4 == 0.9999…`
  cost it a further millisecond); `ondio` cuts `[round(start_sec * sr),
  round(end_sec * sr))` on every path.
- **No ffmpeg.** Decoding and encoding use `soundfile`, whose wheels bundle
  libsndfile. A whole-file decode of a truncated or unknown-length stream now
  raises `OndioError`; ffmpeg used to return the decodable part. A windowed
  re-encode (`decode=False` or `download_flac`) of a 12- or 20-bit source
  raises `OndioError`, because libsndfile writes only 8, 16 and 24 bits;
  `decode=True` still reads it.

## FLAC header parsing

- Legacy's `extract_header` mis-parses the STREAMINFO bit fields: stereo files
  report 1 channel, and 16- and 24-bit files both report 17 bits per sample.
  `ondio.extract_flac_header` parses the block per the FLAC spec (verified
  against libsndfile) and fetches only header bytes, skipping metadata block
  bodies such as embedded artwork instead of downloading them.
