# Objects, files, and JSON

The URI scheme selects the backend. The examples use `s3://`. Local paths,
`file://`, and `gs://` support the same operations. HTTP has fewer operations.

## Backend support

| Operation | Local, S3, GCS | HTTP/HTTPS |
| --- | --- | --- |
| Read, download, check existence | Yes | Yes |
| Write, create, upload | Yes | No |
| List, count, delete | Yes | No |
| Locate | Yes | No |

Unsupported operations raise `ondio.UnsupportedOperationError`. Windowed FLAC
reads over HTTP also need a server that honors range requests; see
[FLAC over HTTP](flac.md#windowed-reads-of-remote-files).

The base install supports local files and S3. Install `ondio[gcs]` for GCS and
`ondio[http]` for HTTP. S3 uses the credentials and configuration available to
boto3. GCS uses the credentials and configuration available to the Google Cloud
Storage client. Configure those credentials before accessing a private bucket.
To pick an AWS profile or another client setting per call, see
[Backend options](#backend-options).

## Bytes in memory

`read` returns the whole object as `bytes`, and `write` stores `bytes` at a URI.
Neither touches the local filesystem.

```python
import ondio

data = ondio.read("s3://my-bucket/audio/rec.flac")
ondio.write("s3://my-bucket/results/example.txt", b"done")
```

## JSON

`write_json` serializes a Python value and writes it; `read_json` reads and parses
one. No local file is involved.

```python
ondio.write_json("s3://my-bucket/results/summary.json", {"detections": 118})
summary = ondio.read_json("s3://my-bucket/results/summary.json")
```

## Files on disk

`download` streams an object to a local path, creating parent directories.
`upload` streams a local file to a URI without reading it into memory, replacing
any object there. The URI always comes first, the local path second.

```python
ondio.download("gs://my-bucket/audio/rec.flac", "data/rec.flac")
ondio.upload("s3://my-bucket/audio/rec.flac", "data/rec.flac")
```

`download_files` downloads many objects into one directory at once. Each file is
named by its basename, and the returned local paths follow the input order.
If two URIs have the same basename, the call raises `ValueError` before downloading.

```python
uris = ondio.list_files("s3://my-bucket/audio/", required_ext="flac")
local = ondio.download_files(uris, "results/", max_workers=8)
```

## Create only if absent

`create` writes bytes only if no object exists at the URI, and a reader never sees
a partial object.

```python
try:
    ondio.create("s3://my-bucket/runs/1/result.json", b"{}")
except ondio.ObjectExistsError:
    ...  # another writer got there first
```

`create` works on S3 (a conditional `PUT` with `If-None-Match: *`), on GCS (a
conditional upload with `if_generation_match=0`), and on local disk (a temporary file
linked into place). HTTP raises `UnsupportedOperationError`, before loading optional
SDKs or initializing credentials. S3 and GCS transport failures raise `OndioError`,
with the original exception as the cause. If `create` fails for any reason other than
`ObjectExistsError`, the object may or may not exist; read it to find out. On S3 and
GCS, `ObjectExistsError` can name an object that the same call wrote, when the client
retried after a lost response; compare the bytes if you need to know who wrote it.

## Where a URI points

`locate` gives the store and the path in it that ondio reads, without touching
storage.

```python
ondio.locate("s3://my-bucket/runs/1/out")
# Location(store='s3://my-bucket', path='runs/1/out')
```

## Listing and counting

`list_files` returns the full URIs under a folder path, sorted. `required_ext` and
`required_prefix` filter by filename, and they compose with `max_items`.
`object_count` counts with the same filters, without listing.

```python
uris = ondio.list_files("s3://my-bucket/results/")
flacs = ondio.list_files("s3://my-bucket/audio/", required_ext="flac")
chunks = ondio.list_files("s3://my-bucket/audio/", required_prefix="chunk_", max_items=100)
n_flacs = ondio.object_count("s3://my-bucket/audio/", required_ext="flac")
```

Listing prefixes have folder semantics on every backend: a path names the
object at that exact location plus everything under it as a folder, never
sibling objects that merely share leading characters —
`s3://my-bucket/results/run-1` matches `results/run-1/…` but **not**
`results/run-10/…`. To match objects by partial *filename* (e.g. everything
starting with `chunk_`), use `required_prefix` as above.

A **trailing slash narrows the path to folder contents only**, excluding an
object stored at the path itself. This matters only where both exist:

```python
ondio.list_files("s3://my-bucket/results/run-1")   # the run-1 object, if any, plus run-1/…
ondio.list_files("s3://my-bucket/results/run-1/")  # only run-1/… — never the run-1 object
```

`object_count(prefix)` always equals `len(list_files(prefix))` given the same
filters, on every backend — folder placeholder objects (the empty `dir/` keys
some consoles create) are counted by neither.

## Checking and deleting

`exists` says whether an object exists. `delete` removes it, and deleting a missing
object does nothing.

```python
if ondio.exists("s3://my-bucket/tmp/scratch.json"):
    ondio.delete("s3://my-bucket/tmp/scratch.json")
```

## Errors

Missing objects raise `ondio.ObjectNotFoundError`. Access failures can raise
`ondio.AuthError`. Both are types of `ondio.OndioError`, which also covers other
storage failures. Catch the specific error when your code can handle it.

## Backend options

Keyword arguments go to the backend's constructor: `boto3.Session` for `s3://`,
`google.cloud.storage.Client` for `gs://`. For example, to pick an AWS profile:

<!-- docs-test: skip (needs a real AWS profile) -->
```python
ondio.list_files("s3://my-bucket/audio/", profile_name="dse")
```

## Endpoints

`endpoint_url=` sets the S3 or GCS endpoint, for example an S3 store that is not
AWS. To make another process (a worker, a batch job) use the same endpoint,
`get_environment` gives the environment variables to set there; that process then
calls ondio with no options:

<!-- docs-test: skip (needs a server at the endpoint) -->
```python
ondio.read("s3://my-bucket/runs/1/out", endpoint_url="http://127.0.0.1:9000")
env = ondio.get_environment("s3://my-bucket/runs/1", endpoint_url="http://127.0.0.1:9000")
# {"AWS_ENDPOINT_URL_S3": "http://127.0.0.1:9000"}: add it to the worker's environment
```

An option that no variable carries (`profile_name`, `region_name`, or `endpoint_url`
on GCS) raises `UnsupportedOperationError`, rather than being dropped. The other
process must not set `AWS_IGNORE_CONFIGURED_ENDPOINT_URLS`, which makes S3 ignore the
endpoint variable.
