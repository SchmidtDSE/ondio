# ondio

`ondio` — _ondas_ (Spanish for waves) + io — is a Python package for uniform IO of
audio data (specifically just `.flac` at the moment) and results derived from
bioacoustic models (specifically `.json` and `.parquet` files) across a variety of
sources: AWS S3, GCS, HTTP/HTTPS, and local filesystems.

Most public functions take a URI. Its scheme selects the storage backend.
Supported operations depend on the backend; HTTP is read-only.

`ondio` is a spin-off of [`soundhub_utils`](https://github.com/SchmidtDSE/soundhub_utils), with just the lightweight components to handle IO from generic backends.

**Documentation: <https://schmidtdse.github.io/ondio/>**

## Install

```bash
pip install ondio            # S3 and local disk
pip install "ondio[gcs]"     # adds gs:// (google-cloud-storage)
pip install "ondio[http]"    # adds http(s):// (requests)
pip install "ondio[parquet]" # adds write_parquet (pyarrow)
```

## Quick start

```python
import ondio

ondio.write_json("results/summary.json", {"detections": 118})
summary = ondio.read_json("results/summary.json")
print(summary)  # {'detections': 118}
```

Use an `s3://` or `gs://` URI for cloud storage after configuring credentials.
Install the corresponding extra for GCS or HTTP. See the
[objects guide](https://schmidtdse.github.io/ondio/guide/objects/) for backend
support and credential options. The [FLAC guide](https://schmidtdse.github.io/ondio/guide/flac/)
explains how to read audio windows and when ondio uses ranged reads.

`read`/`write` move bytes in memory; `download`/`upload` move files on disk. The
[docs](https://schmidtdse.github.io/ondio/) cover every call, FLAC windowed reads,
Parquet datasets, endpoints for other S3 stores, and the differences from
`soundhub_utils`.

## Development

```bash
pixi run -e test test-unit   # fast unit tests
pixi run -e docs docs        # preview the docs site
```
