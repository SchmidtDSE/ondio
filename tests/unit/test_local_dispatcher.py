"""Dispatcher-level tests exercised against the local backend."""

from pathlib import Path

import pytest

import ondio
from ondio import ObjectNotFoundError
from ondio.registry import get_backend


@pytest.fixture
def tree(tmp_path):
    """tmp_path/data with a small file tree."""
    root = tmp_path / "data"
    for rel, content in {
        "chunk_001.flac": b"one",
        "chunk_002.flac": b"two",
        "notes.txt": b"text",
        "sub/chunk_003.flac": b"three",
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


class TestReadWrite:
    def test_write_read_roundtrip(self, tmp_path):
        uri = str(tmp_path / "nested" / "dirs" / "a.bin")
        ondio.write(uri, b"payload")  # creates parents
        assert ondio.read(uri) == b"payload"

    def test_read_missing_raises(self, tmp_path):
        with pytest.raises(ObjectNotFoundError):
            ondio.read(str(tmp_path / "absent"))

    def test_file_uri_addressing(self, tmp_path):
        path = tmp_path / "a.bin"
        ondio.write(path.as_uri(), b"via-file-uri")
        assert ondio.read(str(path)) == b"via-file-uri"

    def test_download(self, tmp_path):
        src = tmp_path / "src.bin"
        src.write_bytes(b"data")
        dest = tmp_path / "out" / "deep" / "copy.bin"
        ondio.download(str(src), dest)
        assert dest.read_bytes() == b"data"

    def test_download_missing_raises(self, tmp_path):
        with pytest.raises(ObjectNotFoundError):
            ondio.download(str(tmp_path / "absent"), tmp_path / "out.bin")


class TestReadRange:
    @pytest.fixture
    def uri(self, tmp_path):
        path = tmp_path / "digits.bin"
        path.write_bytes(b"0123456789")
        return str(path)

    def test_inclusive_range(self, uri):
        assert get_backend(uri).read_range(uri, 2, 5) == b"2345"

    def test_none_means_to_eof(self, uri):
        assert get_backend(uri).read_range(uri, 5, None) == b"56789"
        assert get_backend(uri).read_range(uri, 0, None) == b"0123456789"

    def test_range_past_eof_returns_available(self, uri):
        assert get_backend(uri).read_range(uri, 8, 100) == b"89"
        assert get_backend(uri).read_range(uri, 50, None) == b""

    def test_invalid_ranges(self, uri):
        with pytest.raises(ValueError):
            get_backend(uri).read_range(uri, -1, 5)
        with pytest.raises(ValueError):
            get_backend(uri).read_range(uri, 5, 2)

    def test_size(self, uri):
        assert get_backend(uri).size(uri) == 10


class TestExistsDelete:
    def test_exists(self, tree, tmp_path):
        assert ondio.exists(str(tree / "notes.txt"))
        assert not ondio.exists(str(tmp_path / "absent"))
        assert not ondio.exists(str(tree))  # a directory is not an object

    def test_delete_removes(self, tree):
        target = str(tree / "notes.txt")
        ondio.delete(target)
        assert not ondio.exists(target)

    def test_delete_missing_is_noop(self, tmp_path):
        ondio.delete(str(tmp_path / "absent"))  # no exception


class TestListing:
    def test_recursive(self, tree):
        assert ondio.list_files(str(tree)) == sorted(
            str(tree / name)
            for name in ["chunk_001.flac", "chunk_002.flac", "notes.txt", "sub/chunk_003.flac"]
        )

    def test_non_recursive(self, tree):
        assert ondio.list_files(str(tree), recursive=False) == sorted(
            str(tree / name) for name in ["chunk_001.flac", "chunk_002.flac", "notes.txt"]
        )

    def test_max_items(self, tree):
        assert len(ondio.list_files(str(tree), max_items=2)) == 2

    def test_partial_name_path_never_matches(self, tree):
        # folder semantics: "chunk_" is neither a folder nor an exact file
        assert ondio.list_files(str(tree / "chunk_")) == []
        assert ondio.object_count(str(tree / "chunk_")) == 0

    def test_listing_a_file_yields_that_file(self, tree):
        target = str(tree / "chunk_001.flac")
        assert ondio.list_files(target) == [target]
        assert ondio.object_count(target) == 1
        assert ondio.list_files(target, required_ext="flac") == [target]
        assert ondio.list_files(target, required_ext="txt") == []

    def test_trailing_slash_excludes_the_exact_file(self, tree):
        # a trailing slash names folder contents only, as on the cloud backends
        target = str(tree / "chunk_001.flac")
        assert ondio.list_files(f"{target}/") == []
        assert ondio.object_count(f"{target}/") == 0

    def test_sibling_directories_are_isolated(self, tmp_path):
        for name in ["results/run-1/a.parquet", "results/run-10/b.parquet", "results/run-111/c.parquet"]:
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        assert ondio.list_files(str(tmp_path / "results/run-1")) == [
            str(tmp_path / "results/run-1/a.parquet")
        ]
        assert ondio.object_count(str(tmp_path / "results/run-1")) == 1

    def test_no_matches_is_empty(self, tmp_path):
        assert ondio.list_files(str(tmp_path / "nowhere")) == []

    def test_file_uri_in_file_uris_out(self, tree):
        results = ondio.list_files(tree.as_uri())
        assert results and all(r.startswith("file://") for r in results)

    def test_filename_filters(self, tree):
        flacs = ondio.list_files(str(tree), required_ext="flac")
        assert flacs == [
            str(tree / "chunk_001.flac"),
            str(tree / "chunk_002.flac"),
            str(tree / "sub" / "chunk_003.flac"),  # full-URI sort, nested included
        ]
        assert ondio.list_files(str(tree), required_ext=".flac") == flacs
        assert ondio.list_files(str(tree), required_prefix="chunk_") == flacs
        assert ondio.list_files(str(tree), required_prefix="chunk_", required_ext="flac") == flacs
        assert ondio.list_files(str(tree), required_prefix="chunk_", required_ext="txt") == []

    def test_max_items_applies_after_filters(self, tree):
        assert ondio.list_files(str(tree), required_ext="flac", max_items=2) == [
            str(tree / "chunk_001.flac"),
            str(tree / "chunk_002.flac"),
        ]

    def test_non_recursive_composes_with_filters(self, tree):
        assert ondio.list_files(str(tree), recursive=False, required_ext="flac") == [
            str(tree / "chunk_001.flac"),
            str(tree / "chunk_002.flac"),
        ]

    def test_object_count(self, tree):
        assert ondio.object_count(str(tree)) == 4
        assert ondio.object_count(str(tree), required_ext=".flac") == 3
        assert ondio.object_count(str(tree), required_ext="flac") == 3
        assert ondio.object_count(str(tree), required_prefix="chunk_") == 3
        assert (
            ondio.object_count(str(tree), required_prefix="chunk_", required_ext=".txt") == 0
        )


class TestJsonAndFiles:
    def test_json_roundtrip(self, tmp_path):
        uri = str(tmp_path / "meta.json")
        data = {"id": "rec-7", "tags": ["dawn", "chorus"], "gain_db": -3.5}
        ondio.write_json(uri, data)
        assert ondio.read_json(uri) == data

    def test_upload_from_disk(self, tmp_path):
        src = tmp_path / "src.bin"
        src.write_bytes(b"contents")
        uri = str(tmp_path / "dest" / "copy.bin")
        ondio.upload(uri, src)
        assert ondio.read(uri) == b"contents"

    def test_upload_replaces_existing_file(self, tmp_path):
        first, second = tmp_path / "first.bin", tmp_path / "second.bin"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        uri = str(tmp_path / "dest" / "a.bin")
        ondio.upload(uri, first)
        ondio.upload(uri, second)
        assert ondio.read(uri) == b"second"

    def test_upload_missing_source_raises(self, tmp_path):
        dest = tmp_path / "dest" / "a.bin"
        with pytest.raises(FileNotFoundError):
            ondio.upload(str(dest), tmp_path / "absent.bin")
        assert not dest.exists()

    def test_upload_does_not_read_whole_source(self, tmp_path, monkeypatch):
        src = tmp_path / "src.bin"
        src.write_bytes(b"contents")
        dest = tmp_path / "dest" / "copy.bin"

        def refuse(self):
            raise AssertionError("upload read the whole source into memory")

        monkeypatch.setattr(Path, "read_bytes", refuse)
        ondio.upload(str(dest), src)
        with open(dest, "rb") as f:  # read_bytes is patched out
            assert f.read() == b"contents"
