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
`boto3`. GCS uses the credentials and configuration available to the Google Cloud
Storage client. Configure those credentials before accessing a private bucket.
To pick an AWS profile or another client setting per call, see
[Backend options](#backend-options). To build a backend once and use it for many
calls, see [Reusing a backend](#reusing-a-backend).

## Bytes in memory

`read` returns the whole object as `bytes`, and `write` stores `bytes` at a URI.
Neither touches the local filesystem.

```python
import ondio

data = ondio.read("s3://my-bucket/audio/rec.flac")
ondio.write("s3://my-bucket/results/example.txt", b"done")
```

## Byte ranges

`read_range` reads the bytes from `start_byte` to `end_byte`, both included.
`end_byte=None` reads to the end. A range past the end returns the bytes that
exist, possibly none.

```python
head = ondio.read_range("s3://my-bucket/audio/rec.flac", 0, 3)
assert head == b"fLaC"
```

The result is the stored bytes, not a playable file. For a time window of a FLAC
file, use [`read_flac`](flac.md).

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
A failed `download` leaves no partial file: the data goes to a temporary file
beside the local path, which replaces the local path only when the download is
complete. A file that was already at the local path stays as it was.

```python
ondio.download("gs://my-bucket/audio/rec.flac", "data/rec.flac")
ondio.upload("s3://my-bucket/audio/rec.flac", "data/rec.flac")
```

With `overwrite=False`, `upload` writes the file only if no object exists at the
URI, as [`create`](#create-only-if-absent) writes bytes, and raises
`ObjectExistsError` otherwise. It works on S3, GCS and local disk. On S3 it is one
conditional `PUT`, so the file can be at most 5 GiB; a larger file raises
`UnsupportedOperationError` before anything is sent. As with `create`, on S3 and
GCS `ObjectExistsError` can name the object that the same call wrote, when the
client retried after a lost response.

```python
try:
    ondio.upload("s3://my-bucket/runs/1/rec.flac", "data/rec.flac", overwrite=False)
except ondio.ObjectExistsError:
    ...  # another writer got there first
```

On local disk, `upload` writes to a temporary file beside the target and moves it
into place, so a reader never sees a partly written file. If the target is a
link, the file it points to is replaced and the link stays.

`download_files` downloads many objects into one directory at once. Each file is
named by its basename, and the returned local paths follow the input order.
If two URIs have the same basename, the call raises `ValueError` before downloading.

```python
uris = ondio.list_files("s3://my-bucket/audio/", required_ext="flac")
local = ondio.download_files(uris, "results/", max_workers=8)
```

`upload_files` goes the other way: it uploads every file under a local folder to a
prefix, keeping the relative paths, and returns the URIs sorted by relative path.
Hidden files are uploaded too, and empty folders make no objects. A link to a file
is uploaded with the content it points to; a link to a folder is not entered.
A folder that cannot be read raises `PermissionError`, and an entry that is not a
file, such as a broken link, raises `FileNotFoundError`, both before anything is
uploaded.
`overwrite=False` works as it does for `upload`. The first failure stops the call;
files that already finished stay uploaded.

```python
uris = ondio.upload_files("s3://my-bucket/runs/2", "results/", max_workers=8)
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
linked into place). On HTTP, `HttpBackend` raises `UnsupportedOperationError`.
Missing credentials raise `AuthError`: no request was sent. A connection failure or
timeout raises `NetworkError` with the message "could not confirm create". If `create`
fails for any reason other than `ObjectExistsError` or `AuthError`, the object may or
may not exist; read it to find out. On S3 and GCS, `ObjectExistsError` can name an
object that the same call wrote, when the client retried after a lost response;
compare the bytes if you need to know who wrote it.

## Where a URI points

`locate` gives the store and the path in it that `ondio` reads, without touching
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

Every storage failure raises a type of `ondio.OndioError`. An error from `boto3`,
`google-cloud-storage` or `requests` is translated, and the original exception is
its `__cause__`.

| Error | Meaning |
| --- | --- |
| `ObjectNotFoundError` | No object at the URI |
| `AuthError` | Missing, wrong or expired credentials, or access refused |
| `NetworkError` | No full answer from the store: the connection failed or closed, the response stopped in the middle, or it timed out. The request can be retried |
| `UnsupportedOperationError` | The backend cannot do this, e.g. `write` over HTTP |
| `ObjectExistsError` | `create` found an object at the URI |
| `OndioError` | Any other storage failure |

A bad request raises `ValueError`, not `OndioError`: an inverted byte range, a
FLAC window that is empty, a malformed URI, or an S3 request that `botocore`
refuses before sending it, such as one with an empty key. So code that falls back to another
source can catch `OndioError` for "the fetch failed" and let `ValueError` show a bug.

```python
try:
    data = ondio.read("s3://my-bucket/audio/missing.flac")
except ondio.ObjectNotFoundError:
    data = None
```

A missing extra (for example `ondio[gcs]`) raises `ImportError`.

## Backend options

Keyword arguments go to the backend's constructor: `boto3.Session` for `s3://`,
`google.cloud.storage.Client` for `gs://`. For example, to pick an AWS profile:

<!-- docs-test: skip (needs a real AWS profile) -->
```python
ondio.list_files("s3://my-bucket/audio/", profile_name="dse")
```

For `http(s)://`, the options go to `HttpBackend`; see
[Private HTTPS files](#private-https-files).

## Reusing a backend

Each call builds a new backend from the URI and its options. To use one client and
its connections for many calls, build the backend once and pass it as `backend=`.
Every public function takes it. With `backend=`, other options raise `ValueError`:
give them to the backend instead.

`get_backend` builds the backend for a URI's platform with the options that any
call takes, so you do not need to name its class. The URI only chooses the platform:
the backend serves any URI on that platform, and a URI from another platform raises
`ValueError`.

```python
s3 = ondio.get_backend("s3://my-bucket", region_name="us-east-1")
header = ondio.extract_flac_header("s3://my-bucket/audio/rec.flac", backend=s3)
samples, rate = ondio.read_flac("s3://my-bucket/audio/rec.flac", 10.0, 15.0, backend=s3)
```

The backend classes are in `ondio.backends`. `AwsBackend(client=...)` and
`GcsBackend(client=...)` use a client that you made. No other option can be given
with `client`.

<!-- docs-test: skip (needs GCS credentials) -->
```python
from google.cloud import storage
from ondio.backends import GcsBackend

gcs = GcsBackend(client=storage.Client(project="my-project"))
ondio.read("gs://my-bucket/audio/rec.flac", backend=gcs)
```

`download_files` and `upload_files` give the one backend to all their threads. S3 clients and
`HttpBackend` can be shared that way. A `google-cloud-storage` client does not
promise it; give `download_files` and `upload_files` no GCS backend if that
matters to you.

ondio does not check that the backend matches the URI scheme. A wrong backend fails
at its first call.

## Private HTTPS files

`HttpBackend` takes these options, as keywords:

| Option | Default | Use |
| --- | --- | --- |
| `headers` | none | Headers sent with every request |
| `auth` | none | Anything `requests` takes as `auth`, e.g. `("user", "password")` |
| `cookies` | none | Cookies sent with every request |
| `verify` | `True` | `False` skips the TLS certificate check; a path names a CA bundle |
| `timeout` | `(10, 60)` | Seconds: `(connect, read)`, one number, or `None` for no limit |
| `allow_redirects` | `True` | If `False`, a 3xx answer raises `OndioError` |

A bearer token is a header. When a redirect goes to another host, `ondio` sends
it no `Authorization`, no cookies, and none of the headers you gave. No `ondio` error message holds an option's value.

<!-- docs-test: skip (needs an HTTPS server) -->
```python
from ondio.backends import HttpBackend

web = HttpBackend(headers={"Authorization": "Bearer <token>"}, timeout=(5, 30))
samples, rate = ondio.read_flac("https://example.org/rec.flac", 0.0, 5.0, backend=web)
```

The same options also work per call, without a backend:
`ondio.read(url, headers={...})`. A `Range` header is refused with `ValueError`:
use `read_range`.

The read timeout limits each wait for data, not the whole request. A server that
keeps sending data slowly is not stopped by it.

## Public buckets

To read a public S3 or GCS bucket with no credentials, pass `anonymous=True`:

<!-- docs-test: skip (needs a public bucket) -->
```python
ondio.read("s3://a-public-bucket/rec.flac", anonymous=True)
ondio.list_files("gs://a-public-bucket/audio/", anonymous=True)
```

On S3 the requests are unsigned, and only `endpoint_url` and `region_name` can be
given with it. No profile is used, also not one that `AWS_PROFILE` names. On GCS the client has no credentials and no project, and only
`endpoint_url` can be given with it. Other options raise `ValueError`. A bucket
that is not public then raises `AuthError`, so a caller can try with credentials
first and retry with `anonymous=True` on `AuthError`. For any other client, pass it
as `client=`, for example `AwsBackend(client=my_s3_client)`.

## Tests with a fake backend

`backend=` also takes a test double. A backend is any object with the methods of
[`StorageBackend`](../reference/backends.md), each taking the URI first. This one
serves `s3://` URIs from a local directory:

```python
from pathlib import Path

from ondio.backends import LocalBackend


class S3OnDisk:
    def __init__(self, root):
        self._root = Path(root)
        self._local = LocalBackend()

    def __getattr__(self, name):
        method = getattr(self._local, name)
        return lambda uri, *args, **kwargs: method(
            str(self._root / uri.removeprefix("s3://")), *args, **kwargs
        )


Path("fake/my-bucket").mkdir(parents=True)
Path("fake/my-bucket/a.txt").write_bytes(b"hello")
assert ondio.read("s3://my-bucket/a.txt", backend=S3OnDisk("fake")) == b"hello"
```

`StorageBackend` is part of the public API: a change to its methods is a breaking
change.

## Endpoints

`endpoint_url=` sets the S3 or GCS endpoint, for example an S3 store that is not
AWS. To make another process (a worker, a batch job) use the same endpoint,
`get_environment` gives the environment variables to set there; that process then
calls `ondio` with no options:

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
