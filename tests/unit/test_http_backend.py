"""HTTP backend tests via requests-mock, including the Range contract."""

import io
import threading

import numpy as np
import pytest
import soundfile as sf

pytest.importorskip("requests_mock")
import requests  # noqa: E402

import ondio  # noqa: E402
from ondio import AuthError, ObjectNotFoundError, UnsupportedOperationError  # noqa: E402
from ondio.registry import get_backend  # noqa: E402
from ondio.backends.http import HttpBackend  # noqa: E402

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

    def test_upload_without_overwrite_unsupported(self, tmp_path):
        src = tmp_path / "src.bin"
        src.write_bytes(b"x")
        with pytest.raises(UnsupportedOperationError):
            HttpBackend().upload(URL, src, overwrite=False)

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


class TestFailures:
    @pytest.mark.parametrize("exc", [requests.exceptions.ConnectTimeout,
                                     requests.exceptions.ReadTimeout,
                                     requests.exceptions.ConnectionError])
    def test_transport_failures_are_network_errors(self, requests_mock, exc):
        requests_mock.get(URL, exc=exc)
        requests_mock.head(URL, exc=exc)
        backend = HttpBackend()
        for call in (lambda: backend.read(URL), lambda: backend.exists(URL),
                     lambda: backend.read_range(URL, 0, 1), lambda: backend.size(URL)):
            with pytest.raises(ondio.NetworkError) as raised:
                call()
            assert isinstance(raised.value.__cause__, exc)

    def test_other_request_error_is_an_ondio_error(self, requests_mock, tmp_path):
        requests_mock.get(URL, exc=requests.exceptions.TooManyRedirects)
        with pytest.raises(ondio.OndioError) as raised:
            HttpBackend().download(URL, tmp_path / "a.bin")
        assert not isinstance(raised.value, ondio.NetworkError)

    def test_broken_body_is_a_network_error(self, requests_mock):
        requests_mock.get(URL, exc=requests.exceptions.ChunkedEncodingError)
        with pytest.raises(ondio.NetworkError):
            HttpBackend().read(URL)

    def test_failed_download_leaves_no_partial_file(self, requests_mock, tmp_path):
        class Body(io.RawIOBase):
            def __init__(self):
                self.sent = False

            def readinto(self, buffer):
                if self.sent:
                    raise requests.exceptions.ChunkedEncodingError("cut off")
                self.sent = True
                buffer[:4] = b"part"
                return 4

        dest = tmp_path / "a.bin"
        dest.write_bytes(b"older copy")
        requests_mock.get(URL, body=Body())
        with pytest.raises(ondio.NetworkError):
            HttpBackend().download(URL, dest)
        assert dest.read_bytes() == b"older copy"
        assert [p.name for p in tmp_path.iterdir()] == ["a.bin"]

    def test_download_file_has_normal_permissions(self, requests_mock, tmp_path):
        requests_mock.get(URL, content=CONTENT)
        HttpBackend().download(URL, tmp_path / "a.bin")
        reference = tmp_path / "reference"
        reference.write_bytes(b"")
        assert (tmp_path / "a.bin").stat().st_mode == reference.stat().st_mode

    @pytest.mark.parametrize("url", ["http://", "http://exa mple.com:x/a"])
    def test_bad_url_is_a_value_error(self, url):
        with pytest.raises(ValueError) as raised:
            HttpBackend().read(url)
        assert not isinstance(raised.value, ondio.OndioError)

    @pytest.mark.parametrize("value", [91827, "ab\r\nSECRET-VALUE"])
    def test_bad_header_is_a_value_error_without_its_value(self, requests_mock, value):
        requests_mock.get(URL, content=CONTENT)
        with pytest.raises(ValueError) as raised:
            HttpBackend(headers={"X-A": value}).read(URL)
        assert not isinstance(raised.value, ondio.OndioError)
        assert str(value) not in str(raised.value)
        assert "SECRET-VALUE" not in str(raised.value)


class TestTimeout:
    def test_default_timeout_on_every_request(self, requests_mock, tmp_path):
        serve_with_ranges(requests_mock, URL, CONTENT)
        requests_mock.head(URL, headers={"Content-Length": "10"})
        backend = HttpBackend()
        backend.read(URL)
        backend.read_range(URL, 0, 1)
        backend.size(URL)
        backend.exists(URL)
        backend.download(URL, tmp_path / "a.bin")
        assert len(requests_mock.request_history) == 5
        assert all(r.timeout == (10, 60) for r in requests_mock.request_history)

    def test_exists_fallback_get_has_the_timeout(self, requests_mock):
        requests_mock.head(URL, status_code=405)
        requests_mock.get(URL, content=CONTENT)
        HttpBackend(timeout=3).exists(URL)
        assert [r.timeout for r in requests_mock.request_history] == [3, 3]

    def test_timeout_option(self, requests_mock):
        requests_mock.get(URL, content=CONTENT)
        get_backend(URL, timeout=5).read(URL)
        assert requests_mock.last_request.timeout == 5

    def test_no_timeout(self, requests_mock):
        requests_mock.get(URL, content=CONTENT)
        HttpBackend(timeout=None).read(URL)
        assert requests_mock.last_request.timeout is None


TOKEN = "secret-token-value"


class TestOptions:
    def test_cookies_and_headers_stay_off_another_host(self, requests_mock):
        requests_mock.get(URL, status_code=302, headers={"Location": "http://cdn.example.com/a.bin"})
        requests_mock.get("http://cdn.example.com/a.bin", content=CONTENT)
        backend = HttpBackend(headers={"X-Api-Key": "k"}, cookies={"sid": "s"})
        assert backend.read(URL) == CONTENT
        redirected = requests_mock.last_request
        assert "X-Api-Key" not in redirected.headers
        assert "Cookie" not in redirected.headers

    def test_cookies_and_headers_follow_a_redirect_on_the_same_host(self, requests_mock):
        requests_mock.get(URL, status_code=302, headers={"Location": "http://files.example.com/b.bin"})
        requests_mock.get("http://files.example.com/b.bin", content=CONTENT)
        HttpBackend(headers={"X-Api-Key": "k"}, cookies={"sid": "s"}).read(URL)
        redirected = requests_mock.last_request
        assert redirected.headers["X-Api-Key"] == "k"
        assert "sid=s" in redirected.headers["Cookie"]

    def test_headers_reach_every_request(self, requests_mock, tmp_path):
        serve_with_ranges(requests_mock, URL, CONTENT)
        requests_mock.head(URL, headers={"Content-Length": "10"})
        backend = HttpBackend(headers={"Authorization": f"Bearer {TOKEN}"})
        backend.read(URL)
        backend.size(URL)
        backend.exists(URL)
        backend.download(URL, tmp_path / "a.bin")
        assert backend.read_range(URL, 2, 4) == b"234"
        for request in requests_mock.request_history:
            assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert requests_mock.last_request.headers["Range"] == "bytes=2-4"

    @pytest.mark.parametrize("name", ["Range", "range", "RANGE"])
    def test_a_range_header_is_refused(self, name):
        with pytest.raises(ValueError) as raised:
            HttpBackend(headers={name: "bytes=0-1"})
        assert "bytes=0-1" not in str(raised.value)

    def test_headers_and_cookies_are_copied(self, requests_mock):
        requests_mock.get(URL, content=CONTENT)
        headers, cookies = {"X-A": "1"}, {"c": "1"}
        backend = HttpBackend(headers=headers, cookies=cookies)
        headers["X-A"] = "2"
        cookies["c"] = "2"
        backend.read(URL)
        assert requests_mock.last_request.headers["X-A"] == "1"
        assert requests_mock.last_request.headers["Cookie"] == "c=1"

    def test_cookies_auth_verify(self, requests_mock):
        requests_mock.get(URL, content=CONTENT)
        HttpBackend(cookies={"c": "v"}, auth=("u", "p"), verify="/ca.pem").read(URL)
        request = requests_mock.last_request
        assert "c=v" in request.headers["Cookie"]
        assert request.headers["Authorization"].startswith("Basic ")
        assert request.verify == "/ca.pem"

    def test_options_through_get_backend(self, requests_mock):
        requests_mock.get(URL, content=CONTENT)
        ondio.read(URL, headers={"X-A": "1"}, verify=False)
        assert requests_mock.last_request.headers["X-A"] == "1"
        assert requests_mock.last_request.verify is False

    def test_another_thread_gets_the_same_options(self, requests_mock):
        requests_mock.get(URL, content=CONTENT)
        backend = HttpBackend(headers={"X-A": "1"}, verify=False)
        backend.read(URL)  # builds this thread's session first
        thread = threading.Thread(target=backend.read, args=(URL,))
        thread.start()
        thread.join()
        assert requests_mock.call_count == 2
        assert requests_mock.last_request.headers["X-A"] == "1"
        assert requests_mock.last_request.verify is False

    def test_redirects_are_followed_by_default(self, requests_mock):
        requests_mock.get(URL, status_code=302, headers={"Location": "http://other/a.bin"})
        requests_mock.get("http://other/a.bin", content=CONTENT)
        assert HttpBackend().read(URL) == CONTENT

    def test_redirect_not_followed_is_an_error(self, requests_mock):
        requests_mock.get(URL, status_code=302, headers={"Location": "http://other/"},
                          content=b"<html>moved</html>")
        requests_mock.head(URL, status_code=302, headers={"Location": "http://other/"})
        target = requests_mock.get("http://other/", content=CONTENT)
        backend = HttpBackend(allow_redirects=False)
        with pytest.raises(ondio.OndioError, match="302"):
            backend.read(URL)
        with pytest.raises(ondio.OndioError, match="302"):
            backend.size(URL)
        with pytest.raises(ondio.OndioError, match="302"):
            backend.read_range(URL, 0, 3)
        with pytest.raises(ondio.OndioError, match="302"):
            backend.exists(URL)
        assert target.call_count == 0

    def test_redirect_not_followed_by_exists_fallback_get(self, requests_mock):
        requests_mock.head(URL, status_code=405)
        requests_mock.get(URL, status_code=302, headers={"Location": "http://other/"})
        with pytest.raises(ondio.OndioError, match="302"):
            HttpBackend(allow_redirects=False).exists(URL)

    def test_redirect_not_followed_by_download_leaves_no_file(self, requests_mock, tmp_path):
        requests_mock.get(URL, status_code=302, headers={"Location": "http://other/"},
                          content=b"<html>moved</html>")
        with pytest.raises(ondio.OndioError, match="302"):
            HttpBackend(allow_redirects=False).download(URL, tmp_path / "a.bin")
        assert list(tmp_path.iterdir()) == []

    def test_no_message_holds_a_header_value(self, requests_mock):
        requests_mock.get(URL, status_code=500)
        requests_mock.head(URL, exc=requests.exceptions.ConnectionError(f"Bearer {TOKEN}"))
        backend = HttpBackend(headers={"Authorization": f"Bearer {TOKEN}"})
        for call in (lambda: backend.read(URL), lambda: backend.size(URL)):
            with pytest.raises(ondio.OndioError) as raised:
                call()
            assert TOKEN not in str(raised.value)


def test_read_range_with_options(requests_mock):
    serve_with_ranges(requests_mock, URL, CONTENT)
    assert ondio.read_range(URL, 0, 2, headers={"X-A": "1"}) == b"012"
    assert requests_mock.last_request.headers["X-A"] == "1"
    assert requests_mock.last_request.headers["Range"] == "bytes=0-2"


@pytest.mark.parametrize("verify", [False, "/my-ca.pem"])
def test_verify_wins_over_the_environment_bundle(requests_mock, monkeypatch, verify):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/env-bundle.pem")
    monkeypatch.setenv("CURL_CA_BUNDLE", "/env-bundle.pem")
    requests_mock.get(URL, content=CONTENT)
    requests_mock.head(URL, headers={"Content-Length": "10"})
    backend = HttpBackend(verify=verify)
    backend.read(URL)
    backend.size(URL)
    assert [r.verify for r in requests_mock.request_history] == [verify, verify]
