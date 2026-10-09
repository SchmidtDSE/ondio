import pytest

import ondio
from ondio import UnknownPlatformError, UnsupportedOperationError, detect_platform
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
