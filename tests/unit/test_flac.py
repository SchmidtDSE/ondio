"""FLAC header parsing and read_flac/download_flac tests.

All test windows have integer-millisecond bounds: slicing is at millisecond
granularity, so only such windows can be asserted sample-exact. On the ranged
path, sample-exactness additionally requires the padded fetch to reach back to
t=0 (the 0.5 s pad floor does this for windows starting <= 0.5 s in), so the
decoded blob starts at the true start of audio; mid-stream ranged windows are
only approximately aligned and are asserted as contiguous source audio instead.
"""

import io

import numpy as np
import pytest

sf = pytest.importorskip("soundfile")
from hypothesis import given, strategies as st  # noqa: E402

import ondio  # noqa: E402
from ondio import OndioError  # noqa: E402
from ondio import flac as flac_mod  # noqa: E402
from ondio.registry import get_backend  # noqa: E402


class BytesBackend:
    """In-memory backend for driving flac.py without a filesystem."""

    def __init__(self, data: bytes):
        self.data = data

    def read(self, uri):
        return self.data

    def read_range(self, uri, start_byte, end_byte):
        return self.data[start_byte:] if end_byte is None else self.data[start_byte:end_byte + 1]

    def size(self, uri):
        return len(self.data)


def build_streaminfo(
    *,
    sample_rate,
    channels,
    bits_per_sample,
    total_samples,
    md5=b"\x00" * 16,
    min_block=4096,
    max_block=4096,
):
    block = bytearray(34)
    block[0:2] = min_block.to_bytes(2, "big")
    block[2:4] = max_block.to_bytes(2, "big")
    packed = (
        (sample_rate << 44)
        | ((channels - 1) << 41)
        | ((bits_per_sample - 1) << 36)
        | total_samples
    )
    block[10:18] = packed.to_bytes(8, "big")
    block[18:34] = md5
    return bytes(block)


def build_metadata_region(streaminfo, extra_blocks=()):
    """fLaC marker + STREAMINFO + extra (type, body) blocks, flags per spec."""
    blocks = [(0, streaminfo), *extra_blocks]
    out = b"fLaC"
    for i, (block_type, body) in enumerate(blocks):
        last_flag = 0x80 if i == len(blocks) - 1 else 0
        out += bytes([last_flag | block_type]) + len(body).to_bytes(3, "big") + body
    return out


class TestHeaderParsing:
    """STREAMINFO parsing: a property test over synthetic streams, malformed
    streams, and the public API against real fixtures."""

    @given(
        sample_rate=st.integers(0, 2**20 - 1),
        channels=st.integers(1, 8),
        bits_per_sample=st.integers(1, 32),
        total_samples=st.integers(0, 2**36 - 1),
        md5=st.binary(min_size=16, max_size=16),
        extras=st.lists(
            st.tuples(st.sampled_from([1, 2, 4]), st.integers(0, 20_000)),
            max_size=3,
        ),
    )
    def test_streaminfo_parse_roundtrip(self, sample_rate, channels, bits_per_sample, total_samples, md5, extras):
        streaminfo = build_streaminfo(
            sample_rate=sample_rate,
            channels=channels,
            bits_per_sample=bits_per_sample,
            total_samples=total_samples,
            md5=md5,
        )
        # extras up to 20 KB exercise the beyond-initial-buffer header walk
        region = build_metadata_region(streaminfo, [(t, bytes(n)) for t, n in extras])
        header = flac_mod.extract_flac_header(BytesBackend(region), "mem://a.flac")
        assert header.sample_rate == sample_rate
        assert header.channels == channels
        assert header.bits_per_sample == bits_per_sample
        assert header.total_samples == total_samples
        assert header.md5 == md5
        assert header.audio_data_offset == len(region)

    def test_not_flac_raises(self):
        with pytest.raises(OndioError, match="not a FLAC stream"):
            flac_mod.extract_flac_header(BytesBackend(b"RIFF" + bytes(100)), "mem://a.wav")

    def test_streaminfo_not_first_raises(self):
        streaminfo = build_streaminfo(sample_rate=44100, channels=2, bits_per_sample=16, total_samples=100)
        region = build_metadata_region(streaminfo, [(1, bytes(10))])
        # swap: padding block first
        broken = b"fLaC" + bytes([0x01]) + (10).to_bytes(3, "big") + bytes(10) + region[8:]
        with pytest.raises(OndioError, match="STREAMINFO"):
            flac_mod.extract_flac_header(BytesBackend(broken), "mem://a.flac")

    def test_truncated_metadata_region_raises(self):
        streaminfo = build_streaminfo(sample_rate=44100, channels=2, bits_per_sample=16, total_samples=100)
        # STREAMINFO's header says more blocks follow, but the stream ends
        region = b"fLaC" + bytes([0x00]) + (34).to_bytes(3, "big") + streaminfo
        with pytest.raises(OndioError, match="truncated"):
            flac_mod.extract_flac_header(BytesBackend(region), "mem://a.flac")

    def test_extract_flac_header_public_api(self, fx):
        header = ondio.extract_flac_header(fx.uri)
        assert header.sample_rate == fx.sr
        assert header.channels == fx.channels
        assert header.bits_per_sample == fx.bits
        assert header.total_samples == int(fx.duration * fx.sr)
        assert header.duration == pytest.approx(fx.duration)
        assert header.audio_data_offset > 42
        assert len(header.md5) == 16


class TestWindowReads:
    """Acceptance: real FLAC fixtures, sample-accurate windows (full-download
    path)."""

    @pytest.mark.parametrize(
        ("start_sec", "end_sec"),
        [
            (7.3, 9.9),      # mid-file
            (0.2, 1.5),      # near start
            (0.0, 0.5),      # from zero
            (28.0, 29.95),   # near EOF
            (0.4, 1.4),      # width just under an integer ms in float (0.9999…)
        ],
    )
    def test_window_sample_accurate(self, fx, start_sec, end_sec):
        samples, sample_rate = ondio.read_flac(fx.uri, start_sec, end_sec)
        assert sample_rate == fx.sr
        lo, hi = round(start_sec * fx.sr), round(end_sec * fx.sr)
        assert samples.shape == (hi - lo, fx.channels)
        assert samples.dtype == np.float32
        assert np.array_equal(samples, fx.data[lo:hi])

    def test_open_ended_and_clamped_windows(self, fx):
        tail, _ = ondio.read_flac(fx.uri, 29.0)
        assert np.array_equal(tail, fx.data[29 * fx.sr:])
        head, _ = ondio.read_flac(fx.uri, None, 1.0)
        assert np.array_equal(head, fx.data[:fx.sr])
        clamped, _ = ondio.read_flac(fx.uri, 29.0, 999.0)  # end past EOF clamps
        assert np.array_equal(clamped, fx.data[29 * fx.sr:])

    def test_end_past_eof_clamp_is_logged(self, fx, capsys):
        ondio.read_flac(fx.uri, 29.0, 999.0)
        assert "clamping" in capsys.readouterr().out

    def test_whole_file_decode(self, fx):
        samples, sample_rate = ondio.read_flac(fx.uri)
        assert sample_rate == fx.sr
        assert np.array_equal(samples, fx.data)


class TestDecodeFalse:
    """decode=False: original bytes for the whole file, a standalone lossless
    FLAC for a window."""

    def test_whole_file_decode_false_is_original_bytes(self, fx):
        assert ondio.read_flac(fx.uri, decode=False) == fx.path.read_bytes()

    def test_windowed_decode_false_is_standalone_lossless_flac(self, fx):
        # the remux contract: a windowed slice is a fresh, self-contained FLAC
        # file (naive byte slices are rejected by libsndfile/ffmpeg)
        blob = ondio.read_flac(fx.uri, 7.3, 9.9, decode=False)
        decoded, sample_rate = sf.read(io.BytesIO(blob), dtype="float32", always_2d=True)
        lo, hi = round(7.3 * fx.sr), round(9.9 * fx.sr)
        assert sample_rate == fx.sr
        assert np.array_equal(decoded, fx.data[lo:hi])
        header = flac_mod.extract_flac_header(BytesBackend(blob), "mem://slice.flac")
        assert header.total_samples == hi - lo  # fresh STREAMINFO, not inherited


class TestDownloadFlac:
    def test_download_flac_whole_file_byte_identical(self, fx, tmp_path):
        out = tmp_path / "deep" / "copy.flac"  # parents created
        ondio.download_flac(fx.uri, out)
        assert out.read_bytes() == fx.path.read_bytes()

    def test_download_flac_window_lossless(self, fx, tmp_path):
        out = tmp_path / "clips" / "win.flac"
        ondio.download_flac(fx.uri, out, 7.3, 9.9)
        decoded, sample_rate = sf.read(out, dtype="float32", always_2d=True)
        assert sample_rate == fx.sr
        assert np.array_equal(decoded, fx.data[round(7.3 * fx.sr):round(9.9 * fx.sr)])


class TestValidation:
    @pytest.mark.parametrize(
        ("start_sec", "end_sec", "match"),
        [
            (-1.0, 2.0, "start_sec"),
            (5.0, 3.0, "end_sec"),
            (2.0, 2.0, "end_sec"),
            (31.0, 32.0, "past the end"),
        ],
    )
    def test_invalid_windows_rejected(self, fx, start_sec, end_sec, match):
        with pytest.raises(ValueError, match=match):
            ondio.read_flac(fx.uri, start_sec, end_sec)

    def test_negative_padding_ratio_rejected(self, fx):
        with pytest.raises(ValueError, match="padding_ratio"):
            ondio.read_flac(fx.uri, 1.0, 2.0, padding_ratio=-0.1)

    def test_zero_duration_stream_rejects_windows(self):
        # total_samples = 0 is legal FLAC ("unknown length"), but a windowed
        # read cannot validate or position without a real duration
        streaminfo = build_streaminfo(sample_rate=44100, channels=2, bits_per_sample=16, total_samples=0)
        region = build_metadata_region(streaminfo)
        with pytest.raises(OndioError, match="zero duration"):
            flac_mod.read_flac(BytesBackend(region), "mem://a.flac", 0.0, 1.0)


# The 30 s fixtures are a few MB; ranged fetches of them are well past this,
# while header reads stay under it.
_LARGE = 100_000


class SpyBackend:
    """Delegates to a real backend, counting full reads and large ranged reads."""

    def __init__(self, inner):
        self._inner = inner
        self.full_reads = 0
        self.large_range_reads = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def read(self, uri):
        self.full_reads += 1
        return self._inner.read(uri)

    def read_range(self, uri, start_byte, end_byte):
        data = self._inner.read_range(uri, start_byte, end_byte)
        if len(data) > _LARGE:
            self.large_range_reads += 1
        return data


class RangeSaboteur(SpyBackend):
    """Zeroes out large ranged reads, so a ranged fetch never decodes.
    Small reads (header parsing) pass through."""

    def read_range(self, uri, start_byte, end_byte):
        data = self._inner.read_range(uri, start_byte, end_byte)
        if len(data) > _LARGE:
            self.large_range_reads += 1
            return b"\x00" * len(data)
        return data


class TestFetchStrategy:
    """The heuristic, forced overrides, and the failure contract."""

    def test_heuristic_small_file_full_download(self, fx):
        spy = SpyBackend(get_backend(fx.uri))
        flac_mod.read_flac(spy, fx.uri, 0.75, 2.25)  # file well under 50 MB
        assert spy.full_reads == 1
        assert spy.large_range_reads == 0

    def test_heuristic_big_file_small_window_ranged(self, fx, monkeypatch):
        monkeypatch.setattr(flac_mod, "_RANGE_MIN_FILE_BYTES", _LARGE)
        spy = SpyBackend(get_backend(fx.uri))
        samples, _ = flac_mod.read_flac(spy, fx.uri, 0.4, 1.4)  # 1 s / 30 s < 1/3
        assert spy.full_reads == 0
        assert spy.large_range_reads == 1
        # the 0.5 s pad floor reaches back to t=0, so the blob starts at the
        # true start of audio and this ranged read is sample-exact
        assert np.array_equal(samples, fx.data[round(0.4 * fx.sr):round(1.4 * fx.sr)])

    def test_heuristic_big_file_wide_window_full_download(self, fx, monkeypatch):
        monkeypatch.setattr(flac_mod, "_RANGE_MIN_FILE_BYTES", _LARGE)
        spy = SpyBackend(get_backend(fx.uri))
        flac_mod.read_flac(spy, fx.uri, 5.0, 25.0)  # 20 s / 30 s > 1/3
        assert spy.full_reads == 1
        assert spy.large_range_reads == 0

    @pytest.mark.parametrize(
        ("start_sec", "end_sec"),
        [
            (0.3, 1.5),
            (0.4, 1.4),  # width just under an integer ms in float (0.9999…)
        ],
    )
    def test_use_range_true_forces_ranged_path(self, fx, start_sec, end_sec):
        spy = SpyBackend(get_backend(fx.uri))  # small file: heuristic would say full
        samples, _ = flac_mod.read_flac(spy, fx.uri, start_sec, end_sec, use_range=True)
        assert spy.full_reads == 0
        assert spy.large_range_reads == 1
        # pad floor reaches t=0 for these windows, so the ranged read is exact
        assert np.array_equal(samples, fx.data[round(start_sec * fx.sr):round(end_sec * fx.sr)])

    def test_midstream_ranged_window_is_contiguous_source_audio(self, fx, monkeypatch):
        # mid-stream ranged reads are only approximately aligned (the byte
        # estimate is linear), but what comes back must be a contiguous slice
        # of the real stream, near the requested position — never fabricated
        # audio
        monkeypatch.setattr(flac_mod, "_RANGE_MIN_FILE_BYTES", _LARGE)
        spy = SpyBackend(get_backend(fx.uri))
        samples, _ = flac_mod.read_flac(spy, fx.uri, 7.3, 9.9)
        assert spy.full_reads == 0
        assert spy.large_range_reads == 1
        assert samples.shape == (round(2.6 * fx.sr), fx.channels)
        # locate the returned audio in the source (seeded noise makes it unique)
        probe = samples[:4096, 0]
        candidates = np.flatnonzero(fx.data[: len(fx.data) - probe.size, 0] == probe[0])
        matches = [
            s for s in candidates if np.array_equal(fx.data[s:s + probe.size, 0], probe)
        ]
        assert len(matches) == 1
        # aligned within the padding slack of the requested start
        padding = max(2.6 * 0.25, 0.5)
        assert abs(matches[0] - round(7.3 * fx.sr)) <= round(padding * fx.sr)

    @pytest.mark.parametrize("kwargs", [{}, {"use_range": True}], ids=["heuristic", "forced"])
    def test_undecodable_ranged_fetch_raises_naming_the_fix(self, fx, monkeypatch, kwargs):
        monkeypatch.setattr(flac_mod, "_RANGE_MIN_FILE_BYTES", _LARGE)
        saboteur = RangeSaboteur(get_backend(fx.uri))
        with pytest.raises(OndioError, match="use_range=False"):
            flac_mod.read_flac(saboteur, fx.uri, 0.75, 2.25, **kwargs)
        assert saboteur.large_range_reads == 1  # a single attempt, no retry
        assert saboteur.full_reads == 0  # never an implicit full download

    def test_use_range_false_escape_hatch(self, fx, monkeypatch):
        # the exact fix the OndioError recommends: full download on a backend
        # whose ranged reads are broken
        monkeypatch.setattr(flac_mod, "_RANGE_MIN_FILE_BYTES", _LARGE)
        saboteur = RangeSaboteur(get_backend(fx.uri))
        samples, _ = flac_mod.read_flac(saboteur, fx.uri, 0.75, 2.25, use_range=False)
        assert saboteur.large_range_reads == 0
        assert saboteur.full_reads == 1
        assert np.array_equal(samples, fx.data[round(0.75 * fx.sr):round(2.25 * fx.sr)])

    def test_lying_header_undershoot_raises(self, fx):
        # a truncated recording whose STREAMINFO still claims the full length
        # (real recorder files do this): the byte estimate positions windows
        # near the claimed end past the real audio, and the short decode must
        # raise — never silently return short or fabricated audio
        truncated = fx.path.read_bytes()
        truncated = truncated[: int(len(truncated) * 0.6)]
        backend = BytesBackend(truncated)
        assert flac_mod.extract_flac_header(backend, "mem://t.flac").duration == pytest.approx(30.0)
        with pytest.raises(OndioError, match="use_range=False"):
            flac_mod.read_flac(backend, "mem://t.flac", 27.0, 29.0, use_range=True)

    def test_range_estimate_anchored_past_metadata(self, fx, tmp_path):
        # A 100 KB padding block: the legacy byte-0-anchored estimate would
        # land a short window near t=0 entirely inside it (the real-recorder
        # failure mode); anchoring at audio_data_offset must keep the estimate
        # in the audio region.
        raw = fx.path.read_bytes()
        offset = ondio.extract_flac_header(fx.uri).audio_data_offset
        streaminfo = raw[8:42]
        padded = (
            b"fLaC"
            + bytes([0x00]) + (34).to_bytes(3, "big") + streaminfo
            + bytes([0x81]) + (100_000).to_bytes(3, "big") + bytes(100_000)
            + raw[offset:]
        )
        path = tmp_path / "padded.flac"
        path.write_bytes(padded)
        samples, _ = ondio.read_flac(str(path), 0.5, 1.5, use_range=True)
        assert np.array_equal(samples, fx.data[round(0.5 * fx.sr):round(1.5 * fx.sr)])
