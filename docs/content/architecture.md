# Architecture

`ondio` defines a unified interface for reading/writing files across different platforms, where the target platform is inferred based on the structure of the URI. The URI structure is used to dispatch to platform specific implementations, combining the [strategy](https://en.wikipedia.org/wiki/Strategy_pattern) and [registry](https://martinfowler.com/eaaCatalog/registry.html) design patterns.

Each call resolves a backend from the URI scheme and delegates to it:

![ondio architecture: dispatcher, registry, format helpers and backends](architecture.svg#only-light)
![ondio architecture: dispatcher, registry, format helpers and backends](architecture-dark.svg#only-dark)

- **dispatcher** — what callers import: every function takes a URI, infers the platform, and delegates. No platform types leak out.
- **registry** — the strategy lookup: maps the URI scheme to a platform name and lazily constructs that platform's backend.
- **format helpers** — FLAC and Parquet logic written once against the backend protocol, so partial FLAC reads work identically on S3, GCS, HTTP, and local files. FLAC decoding and encoding use soundfile (libsndfile, bundled in its wheel).
- **backends** — one class per platform implementing the byte-level protocol (`read_range` is what makes partial reads cheap on remote files); each wraps its platform SDK and translates its errors to ondio's exception types.

## Layers

`ondio` follows a layered structure. The primary layer users interact with is the
public API: generic read/write methods plus format-specific ones.

The second layer is the registry, which maps URI schemes to appropriate backends, and
specific helpers for `.flac` and `.parquet` files. File-format specific logic only lives at
this layer — the backends only operate on bytes and arbitrary files on disk, and never
implement format-specific logic.
