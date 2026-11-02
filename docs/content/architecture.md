# Architecture

`ondio` defines a unified interface for reading/writing files across different platforms, where the target platform is inferred based on the structure of the URI. The URI structure is used to dispatch to platform specific implementations, combining the [strategy](https://en.wikipedia.org/wiki/Strategy_pattern) and [registry](https://martinfowler.com/eaaCatalog/registry.html) design patterns.

Each call resolves a backend from the URI scheme and delegates to it. A caller can
also build a backend once and pass it as `backend=`; the call then uses that backend:

![ondio architecture: dispatcher, registry, format helpers and backends](architecture.svg#only-light)
![ondio architecture: dispatcher, registry, format helpers and backends](architecture-dark.svg#only-dark)

- **dispatcher** — what callers import: every function takes a URI, infers the platform, and delegates, or uses the backend given as `backend=`. No platform types leak out: SDK errors arrive as `ondio` errors, with the SDK error as the cause.
- **registry** — the strategy lookup: maps the URI scheme to a platform name and lazily constructs that platform's backend.
- **format helpers** — FLAC and Parquet logic written once against the backend protocol, so partial FLAC reads work identically on S3, GCS, HTTP, and local files. FLAC decoding and encoding use `soundfile` (libsndfile, bundled in its wheel).
- **backends** — one class per platform implementing the byte-level protocol (`read_range` is what makes partial reads cheap on remote files); each wraps its platform SDK and translates its errors to `ondio`'s exception types.

## Layers

`ondio` follows a layered structure. The primary layer users interact with is the
public API: generic read/write methods plus format-specific ones.

The second layer is the registry, which maps URI schemes to appropriate backends, and
specific helpers for `.flac` and `.parquet` files. File-format specific logic only lives at
this layer — the backends only operate on bytes and arbitrary files on disk, and never
implement format-specific logic.

## Ranged FLAC reads

A windowed FLAC read of a large remote file fetches a byte range instead of the
whole object ([when](guide/flac.md#windowed-reads-of-remote-files)). FLAC frames
vary in size ([why](guide/flac.md#how-a-flac-file-is-laid-out)), so the bytes
that hold a time can only be estimated. The read therefore estimates first and
then checks what it fetched:

1. **Estimate.** `ondio` interpolates between the seek table's points to guess
   which bytes hold the window, with some slack on each side. Without a seek
   table, it uses the average bitrate. The estimate only chooses what to fetch.
2. **Find frames.** In the fetched bytes, `ondio` looks for frame headers: a sync
   code, fields that agree with STREAMINFO, and a matching CRC-8. A random byte
   pattern still passes these checks about once in a few thousand, so it keeps
   only a chain of frames whose frame or sample numbers continue one another.
   The headers give the sample position of each frame in the chain.
3. **Widen.** If the chain does not cover a side of the window, that side is
   widened, by more each round, and fetched. Widening fetches only new audio
   bytes. The initial metadata read can overlap the audio range, however, so
   total transfer can exceed the file size.
4. **Decode.** The frames that hold the window are wrapped in a minimal stream
   (the marker and STREAMINFO, then the frames) and decoded with `soundfile`.
   The window is cut from the result by sample index.

A false frame that passes the header checks can still join a chain. The frame's
CRC-16 and decoded sample count provide further checks, but checksums are not
collision-proof and cannot guarantee that every false frame is detected. The
read raises `OndioError`, naming
`use_range=False`, when the chain breaks inside the window, when it cannot cover
the window even at the edge of the audio, or when the decode fails. It never
starts a full download on its own.
