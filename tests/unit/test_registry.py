import pytest

import ondio
from ondio import Location, UnknownPlatformError, UnsupportedOperationError, detect_platform, locate
from ondio.registry import get_backend


@pytest.mark.parametrize(
    ("uri", "platform"),
    [
        ("s3://bucket/key.flac", "aws"),
        ("gs://bucket/key.flac", "gcs"),
        ("http://example.com/a.flac", "url"),
        ("https://example.com/a.flac", "url"),
        # GCS-over-HTTPS stays "url" — users wanting GCS auth use gs://
        ("https://storage.googleapis.com/bucket/key.flac", "url"),
        ("file:///tmp/a.flac", "local"),
        ("/abs/path/a.flac", "local"),
        ("relative/path/a.flac", "local"),
        ("a.flac", "local"),
        ("C:/data/a.flac", "local"),  # drive letter, not a scheme
    ],
)
def test_detect_platform(uri, platform):
    assert detect_platform(uri) == platform


def test_unknown_scheme_raises():
    with pytest.raises(UnknownPlatformError, match="ftp"):
        detect_platform("ftp://host/file")


def test_fresh_backend_per_call(tmp_path):
    # no caching layer: every call constructs a new backend instance
    assert get_backend(str(tmp_path)) is not get_backend(str(tmp_path))


def test_kwargs_forwarded_to_backend_constructor(tmp_path):
    # kwargs go to the backend constructor, so a kwarg it does not accept
    # surfaces immediately as a TypeError
    with pytest.raises(TypeError):
        get_backend(str(tmp_path), bogus_option=1)


@pytest.mark.parametrize("uri", ["gs://bucket/result.json", "http://example.com/result.json", "https://example.com/result.json"])
@pytest.mark.parametrize("setup_error", [ImportError("optional SDK unavailable"), RuntimeError("credentials unavailable")])
def test_create_refuses_unsupported_schemes_before_backend_setup(uri, setup_error, monkeypatch):
    import ondio.dispatcher as dispatcher

    attempted = []

    def unavailable_backend(*args, **kwargs):
        attempted.append(True)
        raise setup_error

    monkeypatch.setattr(dispatcher, "get_backend", unavailable_backend)
    with pytest.raises(UnsupportedOperationError):
        ondio.create(uri, b"x")
    assert attempted == []


@pytest.mark.parametrize(
    ("uri", "location"),
    [
        ("s3://bucket/runs/1/out", Location("s3://bucket", "runs/1/out")),
        ("s3://bucket", Location("s3://bucket", "")),
        ("s3://bucket/", Location("s3://bucket", "")),
        # S3 and GCS keys are read as written: no decoding.
        ("s3://bucket/a/%2e%2e/b", Location("s3://bucket", "a/%2e%2e/b")),
        ("gs://bucket/runs/1/out", Location("gs://bucket", "runs/1/out")),
        ("/data/out", Location("file://", "data/out")),
        ("file:///data/out", Location("file://", "data/out")),
        ("file://localhost/data/out", Location("file://", "data/out")),
        # Local file URIs are decoded, as reads decode them.
        ("file:///data/%6fut", Location("file://", "data/out")),
        ("file:///data/%2e%2e/other", Location("file://", "data/../other")),
        ("file:///data/a%2F..%2Fother", Location("file://", "data/a/../other")),
        ("file:///data/a%20b", Location("file://", "data/a b")),
        # A ? or # reaches a local file name only percent-encoded, or in a bare path.
        ("file:///data/a%3Fb%23c", Location("file://", "data/a?b#c")),
        ("FILE:///data/out", Location("file://", "data/out")),
        ("/data/a?b#c", Location("file://", "data/a?b#c")),
        # The same text in a cloud key stays as written.
        ("s3://bucket/a%3Fb", Location("s3://bucket", "a%3Fb")),
        ("gs://bucket/a%3Fb", Location("gs://bucket", "a%3Fb")),
    ],
)
def test_locate_names_the_store_and_the_path_ondio_reads(uri, location):
    assert locate(uri) == location


@pytest.mark.parametrize(
    "uri",
    [
        "/data/out",
        "/data/out/",
        "file:///data/out",
        "file://localhost/data/out",
        "file:///data/%6fut",
    ],
)
def test_forms_of_one_local_folder_locate_the_same_place(uri):
    assert locate(uri) == Location("file://", "data/out")


@pytest.mark.parametrize(
    ("uri", "error"),
    [
        ("relative/path", ValueError),
        ("file:/data/out", ValueError),
        ("file:data/out", ValueError),
        ("FILE:/data/out", ValueError),
        ("file://host/data/out", ValueError),
        ("s3:///key", ValueError),
        ("gs:///key", ValueError),
        ("https://example.com/a.bin", UnsupportedOperationError),
        ("ftp://host/a.bin", UnknownPlatformError),
    ],
)
def test_locate_refuses_a_location_with_no_fixed_place(uri, error):
    with pytest.raises(error):
        locate(uri)


@pytest.mark.parametrize(
    "uri",
    [
        "file:///data/out?x",
        "file:///data/out#x",
        "file:///data/out?",
        "file:///data/out#",
        "file://localhost/data/out?x",
        "FILE:///data/out?x",
        "s3://bucket/a?b",
        "s3://bucket/a#b",
        "s3://bucket/a?",
        "s3://bucket/a#",
        "gs://bucket/a?b",
        "gs://bucket/a#b",
        "gs://bucket/a?",
        "gs://bucket/a#",
    ],
)
def test_locate_refuses_a_query_or_fragment(uri):
    # urlparse would drop the text after ? or #, so ondio would name another path.
    with pytest.raises(ValueError):
        locate(uri)
