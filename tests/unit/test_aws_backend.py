"""S3 backend tests via moto, driven through the public API."""

import pytest

import ondio
from ondio import ObjectNotFoundError
from ondio.registry import get_backend


@pytest.fixture(autouse=True)
def prefix(aws_bucket):
    """The moto bucket's s3:// prefix (autouse: every test runs inside moto)."""
    return aws_bucket


class TestReadWrite:
    def test_write_read_roundtrip(self, prefix):
        ondio.write(f"{prefix}/a/b.bin", b"payload")
        assert ondio.read(f"{prefix}/a/b.bin") == b"payload"

    def test_read_missing_raises(self, prefix):
        with pytest.raises(ObjectNotFoundError):
            ondio.read(f"{prefix}/absent")

    def test_download(self, prefix, tmp_path):
        uri = f"{prefix}/a.bin"
        ondio.write(uri, b"data")
        dest = tmp_path / "nested" / "a.bin"
        ondio.download(uri, dest)
        assert dest.read_bytes() == b"data"


class TestReadRange:
    def test_read_range_semantics(self, prefix):
        uri = f"{prefix}/digits.bin"
        ondio.write(uri, b"0123456789")
        backend = get_backend(uri)
        assert backend.read_range(uri, 2, 5) == b"2345"
        assert backend.read_range(uri, 5, None) == b"56789"
        assert backend.read_range(uri, 8, 100) == b"89"  # end clamped by S3
        assert backend.read_range(uri, 50, None) == b""  # InvalidRange -> empty
        assert backend.size(uri) == 10

    def test_read_range_invalid_args(self, prefix):
        uri = f"{prefix}/digits.bin"
        ondio.write(uri, b"0123456789")
        backend = get_backend(uri)
        with pytest.raises(ValueError):
            backend.read_range(uri, -1, 5)
        with pytest.raises(ValueError):
            backend.read_range(uri, 5, 2)


class TestExistsDelete:
    def test_exists_and_idempotent_delete(self, prefix):
        uri = f"{prefix}/a.bin"
        ondio.write(uri, b"x")
        assert ondio.exists(uri)
        ondio.delete(uri)
        ondio.delete(uri)  # idempotent, no error
        assert not ondio.exists(uri)


class TestListing:
    @pytest.fixture
    def tree(self, prefix):
        for key, content in {
            "audio/chunk_001.flac": b"one",
            "audio/chunk_002.flac": b"two",
            "audio/notes.txt": b"text",
            "audio/sub/chunk_003.flac": b"three",
        }.items():
            ondio.write(f"{prefix}/{key}", content)

    def test_list_files(self, prefix, tree):
        assert ondio.list_files(f"{prefix}/audio/") == [
            f"{prefix}/audio/chunk_001.flac",
            f"{prefix}/audio/chunk_002.flac",
            f"{prefix}/audio/notes.txt",
            f"{prefix}/audio/sub/chunk_003.flac",
        ]
        assert ondio.list_files(f"{prefix}/audio/", recursive=False) == [
            f"{prefix}/audio/chunk_001.flac",
            f"{prefix}/audio/chunk_002.flac",
            f"{prefix}/audio/notes.txt",
        ]
        assert len(ondio.list_files(f"{prefix}/audio/", max_items=2)) == 2

    def test_folder_semantics_without_trailing_slash(self, prefix, tree):
        # no trailing slash lists the folder's contents, recursive or not
        assert ondio.list_files(f"{prefix}/audio") == ondio.list_files(f"{prefix}/audio/")
        assert ondio.list_files(f"{prefix}/audio", recursive=False) == [
            f"{prefix}/audio/chunk_001.flac",
            f"{prefix}/audio/chunk_002.flac",
            f"{prefix}/audio/notes.txt",
        ]

    def test_partial_name_path_never_matches(self, prefix, tree):
        # folder semantics: "chunk_" is neither a folder nor an exact key
        assert ondio.list_files(f"{prefix}/audio/chunk_") == []
        assert ondio.object_count(f"{prefix}/audio/chunk_") == 0

    def test_listing_a_key_yields_that_key(self, prefix, tree):
        uri = f"{prefix}/audio/chunk_001.flac"
        assert ondio.list_files(uri) == [uri]
        assert ondio.list_files(uri, recursive=False) == [uri]
        assert ondio.object_count(uri) == 1
        assert ondio.list_files(uri, required_ext="flac") == [uri]
        assert ondio.list_files(uri, required_ext="txt") == []

    def test_trailing_slash_excludes_the_exact_key(self, prefix, tree):
        # a trailing slash names folder contents only
        uri = f"{prefix}/audio/chunk_001.flac"
        assert ondio.list_files(f"{uri}/") == []
        assert ondio.object_count(f"{uri}/") == 0

    def test_max_items_counts_the_exact_key(self, prefix, tree):
        # the exact key sorts ahead of everything under it, so the server-side
        # cap stays valid — this breaks if the cap is applied without it
        ondio.write(f"{prefix}/audio", b"an object at the folder's own key")
        assert ondio.list_files(f"{prefix}/audio", max_items=2) == [
            f"{prefix}/audio",
            f"{prefix}/audio/chunk_001.flac",
        ]

    def test_sibling_prefixes_are_isolated(self, prefix):
        for key in ["results/run-1/a.parquet", "results/run-10/b.parquet", "results/run-111/c.parquet"]:
            ondio.write(f"{prefix}/{key}", b"x")
        assert ondio.list_files(f"{prefix}/results/run-1") == [
            f"{prefix}/results/run-1/a.parquet"
        ]
        assert ondio.object_count(f"{prefix}/results/run-1") == 1

    def test_list_files_filename_filters(self, prefix, tree):
        flacs = ondio.list_files(f"{prefix}/audio/", required_ext="flac")
        assert flacs == [
            f"{prefix}/audio/chunk_001.flac",
            f"{prefix}/audio/chunk_002.flac",
            f"{prefix}/audio/sub/chunk_003.flac",  # nested key matched by filename
        ]
        assert ondio.list_files(f"{prefix}/audio/", required_ext=".flac") == flacs
        assert ondio.list_files(f"{prefix}/audio/", required_prefix="chunk_") == flacs
        assert ondio.list_files(f"{prefix}/audio/", required_prefix="chunk_", required_ext="txt") == []

    def test_list_files_max_items_applies_after_filters(self, prefix, tree):
        # keys that sort before the flacs: a native MaxItems cap would fill the
        # page with these and starve the filtered result
        for i in (1, 2, 3):
            ondio.write(f"{prefix}/audio/a{i}.txt", b"")
        assert ondio.list_files(f"{prefix}/audio/", required_ext="flac", max_items=2) == [
            f"{prefix}/audio/chunk_001.flac",
            f"{prefix}/audio/chunk_002.flac",
        ]

    def test_list_files_skips_folder_placeholders(self, prefix, tree):
        ondio.write(f"{prefix}/audio/sub2/", b"")  # console-style folder marker
        listed = ondio.list_files(f"{prefix}/audio/")
        assert f"{prefix}/audio/sub2/" not in listed
        assert len(listed) == 4

    def test_object_count_skips_folder_placeholders(self, prefix, tree):
        ondio.write(f"{prefix}/audio/sub2/", b"")  # console-style folder marker
        assert ondio.object_count(f"{prefix}/audio/") == 4
        # count and listing always agree, placeholders or not
        assert ondio.object_count(f"{prefix}/audio/") == len(ondio.list_files(f"{prefix}/audio/"))

    def test_object_count(self, prefix, tree):
        assert ondio.object_count(f"{prefix}/audio/") == 4
        assert ondio.object_count(f"{prefix}/audio/", required_ext="flac") == 3
        assert ondio.object_count(f"{prefix}/audio/", required_prefix="chunk_") == 3
        assert ondio.object_count(f"{prefix}/audio/", required_prefix="chunk_", required_ext=".txt") == 0


class TestJson:
    def test_json_roundtrip(self, prefix):
        uri = f"{prefix}/meta.json"
        data = {"id": "rec-7", "tags": ["dawn"]}
        ondio.write_json(uri, data)
        assert ondio.read_json(uri) == data
