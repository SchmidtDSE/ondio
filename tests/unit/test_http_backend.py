"""HTTP backend tests via requests-mock, including the Range contract."""

import io

import numpy as np
import pytest

sf = pytest.importorskip("soundfile")
pytest.importorskip("requests_mock")

import ondio  # noqa: E402
from ondio import AuthError, ObjectNotFoundError, UnsupportedOperationError  # noqa: E402
from ondio.registry import get_backend  # noqa: E402

URL = "http://files.example.com/a.bin"
CONTENT = b"0123456789"


def serve_with_ranges(requests_mock, url, content):
    """A GET handler that honors Range like a well-behaved server."""

    def handler(request, context):
        range_header = request.headers.get("Range")
        if range_header is None:
            context.status_code = 200
            return content
        spec = range_header.removeprefix("bytes=")
        start_s, _, end_s = spec.partition("-")
        start = int(start_s)
        if start >= len(content):
            context.status_code = 416
            return b""
        end = min(int(end_s), len(content) - 1) if end_s else len(content) - 1
        context.status_code = 206
        return content[start:end + 1]

    requests_mock.get(url, content=handler)


class TestReadWrite:
    def test_read(self, requests_mock):
        requests_mock.get(URL, content=CONTENT)
        assert ondio.read(URL) == CONTENT

    def test_download(self, requests_mock, tmp_path):
        requests_mock.get(URL, content=CONTENT)
        dest = tmp_path / "sub" / "a.bin"
        ondio.download(URL, dest)
        assert dest.read_bytes() == CONTENT

    def test_writes_and_listing_unsupported(self, requests_mock):
        with pytest.raises(UnsupportedOperationError):
            ondio.write(URL, b"x")
        with pytest.raises(UnsupportedOperationError):
            ondio.delete(URL)
        with pytest.raises(UnsupportedOperationError):
            ondio.list_files("http://files.example.com/")
        with pytest.raises(UnsupportedOperationError):
            ondio.object_count("http://files.example.com/")

    def test_upload_unsupported(self, requests_mock, tmp_path):
        src = tmp_path / "src.bin"
        src.write_bytes(b"x")
        with pytest.raises(UnsupportedOperationError):
            ondio.upload(URL, src)

    def test_create_unsupported(self, requests_mock):
        with pytest.raises(UnsupportedOperationError):
            ondio.create(URL, b"x")


class TestReadRange:
    def test_read_range_honored(self, requests_mock):
        serve_with_ranges(requests_mock, URL, CONTENT)
        backend = get_backend(URL)
        assert backend.read_range(URL, 2, 5) == b"2345"
        assert backend.read_range(URL, 5, None) == b"56789"
        assert backend.read_range(URL, 50, None) == b""  # 416 -> available bytes

    def test_read_range_ignored_by_server_raises(self, requests_mock):
        requests_mock.get(URL, content=CONTENT)  # always answers 200
        backend = get_backend(URL)
        with pytest.raises(UnsupportedOperationError, match="ignored the Range header"):
            backend.read_range(URL, 2, 5)

    def test_size_and_exists(self, requests_mock):
        requests_mock.head(URL, headers={"Content-Length": str(len(CONTENT))})
        backend = get_backend(URL)
        assert backend.size(URL) == len(CONTENT)
        assert ondio.exists(URL)

        missing = "http://files.example.com/absent"
        requests_mock.head(missing, status_code=404)
        assert not ondio.exists(missing)


class TestErrors:
    def test_error_mapping(self, requests_mock):
        requests_mock.get(URL, status_code=404)
        with pytest.raises(ObjectNotFoundError):
            ondio.read(URL)
        requests_mock.get(URL, status_code=403)
        with pytest.raises(AuthError):
            ondio.read(URL)


# ---------------------------------------------------------------------------
# read_flac over HTTP
# ---------------------------------------------------------------------------

FLAC_URL = "http://files.example.com/clip.flac"


def make_flac(seconds=8.0, sr=22050):
    t = np.arange(int(seconds * sr)) / sr
    data = np.stack([np.sin(2 * np.pi * 330 * t)], axis=1) * 0.5
    buf = io.BytesIO()
    sf.write(buf, data, sr, format="FLAC", subtype="PCM_16")
    return buf.getvalue(), sr


class TestReadFlac:
    def test_read_flac_window_over_http_full_download(self, requests_mock):
        flac_bytes, sr = make_flac()
        serve_with_ranges(requests_mock, FLAC_URL, flac_bytes)
        requests_mock.head(FLAC_URL, headers={"Content-Length": str(len(flac_bytes))})
        full, _ = sf.read(io.BytesIO(flac_bytes), dtype="float32", always_2d=True)
        # small file -> the heuristic downloads the whole file, exact result
        samples, out_sr = ondio.read_flac(FLAC_URL, 2.0, 4.0)
        assert out_sr == sr
        assert np.array_equal(samples, full[2 * sr:4 * sr])

    def test_read_flac_window_over_http_forced_range(self, requests_mock):
        flac_bytes, sr = make_flac()
        serve_with_ranges(requests_mock, FLAC_URL, flac_bytes)
        requests_mock.head(FLAC_URL, headers={"Content-Length": str(len(flac_bytes))})
        full, _ = sf.read(io.BytesIO(flac_bytes), dtype="float32", always_2d=True)
        # the 0.5 s pad floor reaches back to t=0 for this window, so even the
        # forced ranged path is sample-exact
        samples, out_sr = ondio.read_flac(FLAC_URL, 0.4, 2.4, use_range=True)
        assert out_sr == sr
        assert np.array_equal(samples, full[round(0.4 * sr):round(2.4 * sr)])

    def test_read_flac_window_needs_ranges(self, requests_mock):
        flac_bytes, _ = make_flac()
        requests_mock.get(FLAC_URL, content=flac_bytes)  # server ignores Range
        # header parsing itself needs byte ranges, so a windowed read fails
        # loudly on such servers regardless of use_range …
        with pytest.raises(UnsupportedOperationError, match="ignored the Range header"):
            ondio.read_flac(FLAC_URL, 2.0, 4.0)
        with pytest.raises(UnsupportedOperationError, match="ignored the Range header"):
            ondio.read_flac(FLAC_URL, 2.0, 4.0, use_range=False)
        # … while whole-file reads never touch the header and still work
        assert ondio.read_flac(FLAC_URL, decode=False) == flac_bytes
