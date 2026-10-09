"""GCS backend tests against an in-memory fake client raising real google
exceptions. (No moto equivalent exists for GCS; the fake covers the client
surface the backend touches, and applies max_results before any filtering —
like the real API — so the filter tests prove the backend bypasses it.)"""

import pytest

pytest.importorskip("google.cloud.storage")
from google.api_core import exceptions as gexc  # noqa: E402

from ondio import AuthError, ObjectNotFoundError, UnsupportedOperationError  # noqa: E402
from ondio.backends.gcs import GcsBackend  # noqa: E402

BUCKET = "testbkt"
PREFIX = f"gs://{BUCKET}"


class FakeBlob:
    def __init__(self, store, name):
        self._store, self.name = store, name

    @property
    def size(self):
        return len(self._store[self.name])

    def _require(self):
        if self.name not in self._store:
            raise gexc.NotFound(f"blob {self.name} not found")

    def download_as_bytes(self, start=None, end=None):
        self._require()
        data = self._store[self.name]
        if start is None:
            return data
        if start >= len(data):
            raise gexc.RequestRangeNotSatisfiable("range start past EOF")
        return data[start:] if end is None else data[start:end + 1]

    def download_to_filename(self, filename):
        self._require()
        with open(filename, "wb") as f:
            f.write(self._store[self.name])

    def upload_from_string(self, data, content_type=None):
        self._store[self.name] = bytes(data, "utf-8") if isinstance(data, str) else bytes(data)

    def upload_from_filename(self, filename, content_type=None):
        with open(filename, "rb") as f:
            self._store[self.name] = f.read()

    def exists(self):
        return self.name in self._store

    def delete(self):
        self._require()
        del self._store[self.name]


class FakeBucket:
    def __init__(self, store):
        self._store = store

    def blob(self, name):
        return FakeBlob(self._store, name)

    def get_blob(self, name):
        return FakeBlob(self._store, name) if name in self._store else None


class FakeClient:
    def __init__(self, store):
        self._store = store

    def bucket(self, name):
        return FakeBucket(self._store)

    def list_blobs(self, bucket, prefix=None, delimiter=None, max_results=None):
        names = sorted(n for n in self._store if prefix is None or n.startswith(prefix))
        if delimiter:
            names = [n for n in names if delimiter not in n[len(prefix or ""):]]
        if max_results is not None:
            names = names[:max_results]
        return [FakeBlob(self._store, n) for n in names]


@pytest.fixture
def store():
    return {
        "audio/chunk_001.flac": b"one",
        "audio/chunk_002.flac": b"two",
        "audio/notes.txt": b"text",
        "audio/sub/chunk_003.flac": b"three",
    }


@pytest.fixture
def backend(store):
    instance = GcsBackend.__new__(GcsBackend)
    instance._client = FakeClient(store)
    return instance


class TestReadWrite:
    def test_read(self, backend):
        assert backend.read(f"{PREFIX}/audio/chunk_001.flac") == b"one"

    def test_read_missing_raises(self, backend):
        with pytest.raises(ObjectNotFoundError):
            backend.read(f"{PREFIX}/audio/absent.flac")

    def test_write_roundtrip(self, backend):
        backend.write(f"{PREFIX}/new/x.bin", b"payload")
        assert backend.read(f"{PREFIX}/new/x.bin") == b"payload"

    def test_download(self, backend, tmp_path):
        dest = tmp_path / "sub" / "out.bin"
        backend.download(f"{PREFIX}/audio/chunk_001.flac", dest)
        assert dest.read_bytes() == b"one"

    def test_upload_does_not_upload_from_string(self, backend, store, tmp_path, monkeypatch):
        def refuse(*args, **kwargs):
            raise AssertionError("upload went through upload_from_string")

        monkeypatch.setattr(FakeBlob, "upload_from_string", refuse)
        src = tmp_path / "src.bin"
        src.write_bytes(b"contents")
        backend.upload(f"{PREFIX}/new/x.bin", src)
        assert store["new/x.bin"] == b"contents"


class TestReadRange:
    def test_read_range_semantics(self, backend, store):
        store["digits.bin"] = b"0123456789"
        uri = f"{PREFIX}/digits.bin"
        assert backend.read_range(uri, 2, 5) == b"2345"
        assert backend.read_range(uri, 5, None) == b"56789"
        assert backend.read_range(uri, 50, None) == b""  # 416 -> available bytes
        assert backend.size(uri) == 10

    def test_read_range_invalid_args(self, backend, store):
        store["digits.bin"] = b"0123456789"
        uri = f"{PREFIX}/digits.bin"
        with pytest.raises(ValueError):
            backend.read_range(uri, -1, 5)
        with pytest.raises(ValueError):
            backend.read_range(uri, 5, 2)


class TestExistsDelete:
    def test_exists_and_idempotent_delete(self, backend):
        uri = f"{PREFIX}/audio/notes.txt"
        assert backend.exists(uri)
        backend.delete(uri)
        backend.delete(uri)  # idempotent
        assert not backend.exists(uri)


class TestListing:
    def test_list_files(self, backend):
        assert backend.list_files(f"{PREFIX}/audio/") == [
            f"{PREFIX}/audio/chunk_001.flac",
            f"{PREFIX}/audio/chunk_002.flac",
            f"{PREFIX}/audio/notes.txt",
            f"{PREFIX}/audio/sub/chunk_003.flac",
        ]
        assert backend.list_files(f"{PREFIX}/audio/", recursive=False) == [
            f"{PREFIX}/audio/chunk_001.flac",
            f"{PREFIX}/audio/chunk_002.flac",
            f"{PREFIX}/audio/notes.txt",
        ]
        assert len(backend.list_files(f"{PREFIX}/audio/", max_items=2)) == 2

    def test_folder_semantics_without_trailing_slash(self, backend):
        # no trailing slash lists the folder's contents, recursive or not
        assert backend.list_files(f"{PREFIX}/audio") == backend.list_files(f"{PREFIX}/audio/")
        assert backend.list_files(f"{PREFIX}/audio", recursive=False) == [
            f"{PREFIX}/audio/chunk_001.flac",
            f"{PREFIX}/audio/chunk_002.flac",
            f"{PREFIX}/audio/notes.txt",
        ]

    def test_partial_name_path_never_matches(self, backend):
        # folder semantics: "chunk_" is neither a folder nor an exact key
        assert backend.list_files(f"{PREFIX}/audio/chunk_") == []
        assert backend.object_count(f"{PREFIX}/audio/chunk_") == 0

    def test_listing_a_key_yields_that_key(self, backend):
        uri = f"{PREFIX}/audio/chunk_001.flac"
        assert backend.list_files(uri) == [uri]
        assert backend.list_files(uri, recursive=False) == [uri]
        assert backend.object_count(uri) == 1
        assert backend.list_files(uri, required_ext="flac") == [uri]
        assert backend.list_files(uri, required_ext="txt") == []

    def test_trailing_slash_excludes_the_exact_key(self, backend):
        # a trailing slash names folder contents only
        uri = f"{PREFIX}/audio/chunk_001.flac"
        assert backend.list_files(f"{uri}/") == []
        assert backend.object_count(f"{uri}/") == 0

    def test_sibling_prefixes_are_isolated(self, backend, store):
        store["results/run-1/a.parquet"] = b""
        store["results/run-10/b.parquet"] = b""
        assert backend.list_files(f"{PREFIX}/results/run-1") == [
            f"{PREFIX}/results/run-1/a.parquet"
        ]
        assert backend.object_count(f"{PREFIX}/results/run-1") == 1

    def test_list_files_filename_filters(self, backend):
        flacs = backend.list_files(f"{PREFIX}/audio/", required_ext="flac")
        assert flacs == [
            f"{PREFIX}/audio/chunk_001.flac",
            f"{PREFIX}/audio/chunk_002.flac",
            f"{PREFIX}/audio/sub/chunk_003.flac",  # nested key matched by filename
        ]
        assert backend.list_files(f"{PREFIX}/audio/", required_ext=".flac") == flacs
        assert backend.list_files(f"{PREFIX}/audio/", required_prefix="chunk_") == flacs
        assert backend.list_files(f"{PREFIX}/audio/", required_prefix="chunk_", required_ext="txt") == []

    def test_list_files_max_items_applies_after_filters(self, backend, store):
        # names that sort before the flacs: a native max_results cap would fill
        # the page with these and starve the filtered result
        store["audio/a1.txt"] = b""
        store["audio/a2.txt"] = b""
        assert backend.list_files(f"{PREFIX}/audio/", required_ext="flac", max_items=2) == [
            f"{PREFIX}/audio/chunk_001.flac",
            f"{PREFIX}/audio/chunk_002.flac",
        ]

    def test_list_files_skips_folder_placeholders(self, backend, store):
        store["audio/dir/"] = b""  # console-style folder marker
        listed = backend.list_files(f"{PREFIX}/audio/")
        assert f"{PREFIX}/audio/dir/" not in listed
        assert len(listed) == 4

    def test_object_count_skips_folder_placeholders(self, backend, store):
        store["audio/dir/"] = b""  # console-style folder marker
        assert backend.object_count(f"{PREFIX}/audio/") == 4
        # count and listing always agree, placeholders or not
        assert backend.object_count(f"{PREFIX}/audio/") == len(backend.list_files(f"{PREFIX}/audio/"))

    def test_object_count(self, backend):
        assert backend.object_count(f"{PREFIX}/audio/") == 4
        assert backend.object_count(f"{PREFIX}/audio/", required_ext="flac") == 3
        assert backend.object_count(f"{PREFIX}/audio/", required_prefix="chunk_") == 3


class TestErrors:
    def test_auth_error_mapping(self, backend, monkeypatch):
        def deny(*args, **kwargs):
            raise gexc.Forbidden("nope")

        monkeypatch.setattr(FakeBlob, "download_as_bytes", deny)
        with pytest.raises(AuthError):
            backend.read(f"{PREFIX}/audio/chunk_001.flac")

    def test_upload_auth_error_mapping(self, backend, monkeypatch, tmp_path):
        def deny(*args, **kwargs):
            raise gexc.Forbidden("nope")

        monkeypatch.setattr(FakeBlob, "upload_from_filename", deny)
        src = tmp_path / "src.bin"
        src.write_bytes(b"contents")
        with pytest.raises(AuthError):
            backend.upload(f"{PREFIX}/new/x.bin", src)


def test_create_is_unsupported(backend):
    with pytest.raises(UnsupportedOperationError):
        backend.create(f"{PREFIX}/result.json", b"x")
