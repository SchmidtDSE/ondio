"""FLAC header parsing and read_flac/download_flac tests.

Windows are sample-exact on every path: a window is the source's samples
[round(start_sec * sr), round(end_sec * sr)), whether it was cut from a full
download or located frame by frame in a ranged fetch. Damaged input raises
OndioError rather than returning short or misplaced audio.
"""

import io
import tracemalloc
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from hypothesis import given, strategies as st

import ondio
from ondio import OndioError
from ondio import flac as flac_mod
from ondio._flac_frames import find_frame_chain, parse_layout
from ondio.registry import get_backend

from flac_helpers import BytesBackend, build_metadata_region, build_streaminfo


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

    def test_malformed_streams_raise(self):
        streaminfo = build_streaminfo(sample_rate=44100, channels=2, bits_per_sample=16, total_samples=100)
        region = build_metadata_region(streaminfo, [(1, bytes(10))])
        cases = {
            "not a FLAC stream": b"RIFF" + bytes(100),
            # padding block first
            "STREAMINFO": b"fLaC" + bytes([0x01]) + (10).to_bytes(3, "big") + bytes(10) + region[8:],
            # STREAMINFO's header says more blocks follow, but the stream ends
            "truncated": b"fLaC" + bytes([0x00]) + (34).to_bytes(3, "big") + streaminfo,
        }
        for match, data in cases.items():
            with pytest.raises(OndioError, match=match):
                flac_mod.extract_flac_header(BytesBackend(data), "mem://a.flac")

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

    def test_whole_file_decode(self, fx):
        samples, sample_rate = ondio.read_flac(fx.uri)
        assert sample_rate == fx.sr
        assert np.array_equal(samples, fx.data)


class TestSampleExactFullPath:
    """soundfile on the whole-file and full-download paths: windows cut by
    sample index, damaged input raises OndioError."""

    @pytest.mark.parametrize(("s", "e"), [(7.30001, 9.9), (1 / 48000, 3 / 48000), (0.0, 30.0)])
    def test_non_ms_windows_exact(self, fx, s, e):
        samples, _ = ondio.read_flac(fx.uri, s, e, use_range=False)
        assert np.array_equal(samples, fx.data[round(s * fx.sr):round(e * fx.sr)])

    def test_corrupt_far_from_window_ok(self, fx_damaged):
        samples, _ = ondio.read_flac(fx_damaged.corrupt, 1.0, 3.0, use_range=False)
        assert np.array_equal(samples, fx_damaged.data[48000:3 * 48000])

    def test_corrupt_in_window_raises(self, fx_damaged):
        with pytest.raises(OndioError):
            ondio.read_flac(fx_damaged.corrupt, 13.0, 17.0, use_range=False)

    def test_truncated_inside_ok_past_end_raises(self, fx_damaged):
        samples, _ = ondio.read_flac(fx_damaged.truncated, 2.0, 4.0, use_range=False)
        assert np.array_equal(samples, fx_damaged.data[2 * 48000:4 * 48000])
        for s, e in [(17.0, 19.0), (25.0, 27.0)]:
            with pytest.raises(OndioError):
                ondio.read_flac(fx_damaged.truncated, s, e, use_range=False)

    def test_whole_file_truncated_raises(self, fx_damaged):
        with pytest.raises(OndioError):
            ondio.read_flac(fx_damaged.truncated)

    def test_whole_file_unknown_length_raises(self, fx):
        raw = bytearray(fx.path.read_bytes())
        packed = int.from_bytes(raw[18:26], "big") & ~((1 << 36) - 1)
        raw[18:26] = packed.to_bytes(8, "big")
        with pytest.raises(OndioError):
            flac_mod.read_flac(BytesBackend(bytes(raw)), "mem://u.flac")

    @pytest.mark.parametrize(
        "kwargs",
        [{}, {"decode": False}, {"use_range": True}, {"use_range": True, "decode": False}],
        ids=["decode", "bytes", "ranged", "ranged-bytes"],
    )
    def test_sub_sample_window_raises(self, fx, kwargs):
        with pytest.raises(ValueError, match="no samples"):
            ondio.read_flac(fx.uri, 1.0, 1.0 + 1e-6, **kwargs)

    def test_sub_sample_window_download_raises(self, fx, tmp_path):
        out = tmp_path / "empty.flac"
        with pytest.raises(ValueError, match="no samples"):
            ondio.download_flac(fx.uri, out, 1.0, 1.0 + 1e-6)
        assert not out.exists()

    def test_whole_file_decode_memory(self, fx):
        # peak is the fetched bytes plus the float32 result, not extra int32
        # and float64 copies of the whole stream
        tracemalloc.start()
        samples, _ = ondio.read_flac(fx.uri)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert peak <= fx.path.stat().st_size + 1.5 * samples.nbytes

    def test_whole_file_rejects_non_flac(self, tmp_path):
        path = tmp_path / "a.wav"
        sf.write(path, np.zeros((4800, 1)), 48000, format="WAV")
        with pytest.raises(OndioError, match="not a FLAC stream"):
            ondio.read_flac(str(path))

    def test_reencode_rejects_unsupported_depth(self):
        # The advice must hold for download_flac too, which has no decode argument.
        with pytest.raises(OndioError, match=r"read_flac\(\.\.\., decode=True\)"):
            flac_mod._encode_flac(np.zeros((10, 1), np.int32), 48000, 20)


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

# The metadata walk's first read, which can overlap the first audio bytes.
_METADATA_READ = 4096


class SpyBackend:
    """Delegates to a real backend, counting full reads and ranged reads."""

    def __init__(self, inner):
        self._inner = inner
        self.full_reads = 0
        self.range_reads = 0
        self.large_range_reads = 0
        self.bytes_ranged = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def read(self, uri):
        self.full_reads += 1
        return self._inner.read(uri)

    def read_range(self, uri, start_byte, end_byte):
        data = self._inner.read_range(uri, start_byte, end_byte)
        self.range_reads += 1
        self.bytes_ranged += len(data)
        if len(data) > _LARGE:
            self.large_range_reads += 1
        return data


class HeaderCounter(SpyBackend):
    """Counts reads of the stream's first bytes (the metadata walk)."""

    def __init__(self, inner):
        super().__init__(inner)
        self.header_reads = 0

    def read_range(self, uri, start_byte, end_byte):
        if start_byte == 0:
            self.header_reads += 1
        return super().read_range(uri, start_byte, end_byte)


class RangeSaboteur(SpyBackend):
    """Zeroes out large ranged reads, so a ranged fetch never decodes.
    Small reads (header parsing) pass through."""

    def read_range(self, uri, start_byte, end_byte):
        data = super().read_range(uri, start_byte, end_byte)
        return b"\x00" * len(data) if len(data) > _LARGE else data


class ShortLeftRange(SpyBackend):
    """Returns one byte too few on any ranged read that starts left of the
    first large fetch, i.e. when a ranged read widens to the left."""

    def __init__(self, inner):
        super().__init__(inner)
        self.first_lo = None
        self.short_reads = 0

    def read_range(self, uri, start_byte, end_byte):
        data = super().read_range(uri, start_byte, end_byte)
        if self.first_lo is None and len(data) > _LARGE:
            self.first_lo = start_byte
        elif self.first_lo is not None and start_byte < self.first_lo:
            self.short_reads += 1
            return data[:-1]
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
        assert np.array_equal(samples, fx.data[round(start_sec * fx.sr):round(end_sec * fx.sr)])

    def test_midstream_ranged_window_is_exact(self, fx, monkeypatch):
        monkeypatch.setattr(flac_mod, "_RANGE_MIN_FILE_BYTES", _LARGE)
        spy = SpyBackend(get_backend(fx.uri))
        samples, _ = flac_mod.read_flac(spy, fx.uri, 7.3, 9.9)
        assert spy.full_reads == 0
        assert spy.large_range_reads == 1  # uniform bitrate: the first fetch covers it
        assert np.array_equal(samples, fx.data[round(7.3 * fx.sr):round(9.9 * fx.sr)])

    @pytest.mark.parametrize("kwargs", [{}, {"use_range": True}], ids=["heuristic", "forced"])
    def test_undecodable_ranged_fetch_raises_naming_the_fix(self, fx, monkeypatch, kwargs):
        monkeypatch.setattr(flac_mod, "_RANGE_MIN_FILE_BYTES", _LARGE)
        saboteur = RangeSaboteur(get_backend(fx.uri))
        with pytest.raises(OndioError, match="use_range=False"):
            flac_mod.read_flac(saboteur, fx.uri, 0.75, 2.25, **kwargs)
        # The zeroed fetches hold no frames, so the read widens to the edges of
        # the audio, fetching each byte once, and raises there.
        assert saboteur.large_range_reads >= 1
        assert saboteur.bytes_ranged <= fx.path.stat().st_size + _METADATA_READ
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

    @pytest.mark.parametrize("decode", [True, False])
    def test_header_fetched_once(self, fx, decode):
        spy = HeaderCounter(get_backend(fx.uri))
        flac_mod.read_flac(spy, fx.uri, 7.3, 9.9, decode=decode, use_range=True)
        assert spy.header_reads == 1

    def test_lying_header_undershoot_raises(self, fx):
        # a truncated recording whose STREAMINFO still claims the full length
        # (real recorder files do this): a window past the real audio must
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


def with_seektable(raw: bytes, every: int) -> bytes:
    """`raw` with a SEEKTABLE pointing at the first frame of every `every` samples."""
    layout = parse_layout(BytesBackend(raw), "mem://a.flac", seektable=False)
    audio = raw[layout.audio_offset:]
    chain = find_frame_chain(audio, 0, layout)
    points, target = b"", 0
    for frame in chain:
        if frame.first_sample >= target:
            points += (
                frame.first_sample.to_bytes(8, "big")
                + frame.offset.to_bytes(8, "big")
                + frame.block_size.to_bytes(2, "big")
            )
            target = frame.first_sample + every
    return build_metadata_region(layout.streaminfo, [(3, points)]) + audio


@pytest.fixture(scope="session")
def fx_vbr_seektable(fx_vbr, tmp_path_factory):
    path = tmp_path_factory.mktemp("flac_vbr_st") / "vbr_seektable.flac"
    path.write_bytes(with_seektable(fx_vbr.path.read_bytes(), 48_000))
    return SimpleNamespace(**{**vars(fx_vbr), "path": path, "uri": str(path)})


@pytest.fixture(scope="session")
def fx_truncated_seektable(fx_vbr, tmp_path_factory):
    raw = with_seektable(fx_vbr.path.read_bytes(), 48_000)
    path = tmp_path_factory.mktemp("flac_trunc_st") / "truncated_seektable.flac"
    path.write_bytes(raw[: int(len(raw) * 0.6)])
    return SimpleNamespace(**{**vars(fx_vbr), "path": path, "uri": str(path)})


_RANGED_WINDOWS = [
    (7.3, 9.9),
    (15.0, 15.0 + 1 / 48000),  # one sample
    (12.0, 12.05),  # shorter than one block
    (0.0, 0.5),
    (29.5, 30.0),  # ends exactly at EOF
    (29.0, 999.0),  # clamped past EOF
]


def _ranged_vs_full(f, s, e):
    spy = SpyBackend(get_backend(f.uri))
    ranged, _ = flac_mod.read_flac(spy, f.uri, s, e, use_range=True)
    assert spy.full_reads == 0
    full, _ = ondio.read_flac(f.uri, s, e, use_range=False)
    assert len(full) > 0
    assert np.array_equal(ranged, full)
    return spy


class TestRangedExact:
    """The ranged path locates real frames and returns exactly the samples
    a full download does."""

    @pytest.mark.parametrize(("s", "e"), _RANGED_WINDOWS)
    def test_ranged_equals_full_fx(self, fx, s, e):
        _ranged_vs_full(fx, s, e)

    @pytest.mark.parametrize("name", ["fx_vbr", "fx_vbr_seektable"])
    @pytest.mark.parametrize(("s", "e"), _RANGED_WINDOWS)
    def test_ranged_equals_full_vbr(self, request, name, s, e):
        _ranged_vs_full(request.getfixturevalue(name), s, e)

    def test_seektable_fixture_has_points(self, fx_vbr_seektable):
        layout = parse_layout(get_backend(fx_vbr_seektable.uri), fx_vbr_seektable.uri, seektable=True)
        assert len(layout.seekpoints) >= 29

    @pytest.mark.parametrize(("s", "e"), [(17.0, 19.0), (20.0, 21.0)])
    def test_truncated_seektable_inside_exact(self, fx_vbr, fx_truncated_seektable, s, e):
        f = fx_truncated_seektable
        _ranged_vs_full(f, s, e)
        samples, _ = ondio.read_flac(f.uri, s, e, use_range=True)
        assert np.array_equal(samples, fx_vbr.data[round(s * f.sr):round(e * f.sr)])

    def test_truncated_seektable_past_end_raises(self, fx_truncated_seektable):
        f = fx_truncated_seektable
        with pytest.raises(OndioError, match="use_range=False"):
            ondio.read_flac(f.uri, 22.0, 23.0, use_range=True)
        with pytest.raises(OndioError):
            ondio.read_flac(f.uri, 22.0, 23.0, use_range=False)

    def test_block_boundaries(self, fx_vbr):
        sr = fx_vbr.sr
        block = parse_layout(get_backend(fx_vbr.uri), fx_vbr.uri, seektable=False).max_blocksize
        for k in (3, 117, 200, 340):
            for ds in (-1, 0, 1):
                for n in (1, block - 1, block, block + 1):
                    start = k * block + ds
                    samples, _ = ondio.read_flac(fx_vbr.uri, start / sr, (start + n) / sr, use_range=True)
                    assert np.array_equal(samples, fx_vbr.data[start:start + n]), (k, ds, n)

    def test_fetch_bounded_without_seektable(self, fx_vbr):
        spy = _ranged_vs_full(fx_vbr, 20.0, 21.0)
        assert spy.bytes_ranged <= fx_vbr.path.stat().st_size + _METADATA_READ

    @pytest.mark.parametrize(("s", "e"), [(13.0, 17.0), (14.9, 15.1)])
    def test_corrupt_window_raises_names_fix(self, fx_damaged, s, e):
        # The damage is inside the fetched bytes, so widening cannot help: the
        # read raises at once, naming the damage, without widening to an edge.
        spy = SpyBackend(get_backend(fx_damaged.corrupt))
        with pytest.raises(OndioError, match="use_range=False") as exc:
            flac_mod.read_flac(spy, fx_damaged.corrupt, s, e, use_range=True)
        assert "stops inside" in str(exc.value)
        assert spy.range_reads == 2  # the metadata, then one fetch: no widening

    def test_corrupt_far_window_exact(self, fx_damaged):
        samples, _ = ondio.read_flac(fx_damaged.corrupt, 1.0, 3.0, use_range=True)
        assert np.array_equal(samples, fx_damaged.data[48000:3 * 48000])
        samples, _ = ondio.read_flac(fx_damaged.corrupt, 26.0, 28.0, use_range=True)
        assert np.array_equal(samples, fx_damaged.data[26 * 48000:28 * 48000])

    def test_truncated(self, fx_damaged):
        samples, _ = ondio.read_flac(fx_damaged.truncated, 2.0, 4.0, use_range=True)
        assert np.array_equal(samples, fx_damaged.data[2 * 48000:4 * 48000])
        for s, e in [(17.0, 19.0), (25.0, 27.0)]:
            with pytest.raises(OndioError, match="use_range=False"):
                ondio.read_flac(fx_damaged.truncated, s, e, use_range=True)

    def test_short_range_when_widening_raises(self, fx_vbr):
        backend = ShortLeftRange(get_backend(fx_vbr.uri))
        with pytest.raises(OndioError, match="use_range=False"):
            flac_mod.read_flac(backend, fx_vbr.uri, 7.3, 9.9, use_range=True)
        assert backend.short_reads >= 1  # the read did widen to the left

    def test_widening_scans_each_byte_once(self, fx_vbr, monkeypatch):
        scanned = []
        real = flac_mod.scan_frames

        def counting(buf, base, layout, start=0, stop=None):
            scanned.append((len(buf) if stop is None else stop) - start)
            return real(buf, base, layout, start, stop)

        monkeypatch.setattr(flac_mod, "scan_frames", counting)
        spy = SpyBackend(get_backend(fx_vbr.uri))
        flac_mod.read_flac(spy, fx_vbr.uri, 7.3, 9.9, use_range=True)  # widens (no seek table)
        assert len(scanned) >= 2
        assert sum(scanned) <= spy.bytes_ranged + 16 * len(scanned)


def _damage_frame(raw: bytes, sample: int, *, header: bool) -> bytes:
    """`raw` with the frame holding `sample` damaged: one header byte
    (CRC-8 fails) or 50 body bytes (header intact, CRC-16 fails)."""
    layout = parse_layout(BytesBackend(raw), "mem://a.flac", seektable=False)
    chain = find_frame_chain(raw[layout.audio_offset:], layout.audio_offset, layout)
    frame = next(f for f in chain if f.first_sample <= sample < f.end_sample)
    out = bytearray(raw)
    if header:
        out[frame.offset + 2] ^= 0x01
    else:
        mid = frame.offset + 200
        for i in range(mid, mid + 50):
            out[i] ^= 0x5A
    return bytes(out)


class TestRangedNearDamage:
    """Windows clear of a damaged frame read exactly on the ranged path too,
    even when the damage sits inside the fetched bytes."""

    @pytest.mark.parametrize("header", [False, True], ids=["body", "header"])
    @pytest.mark.parametrize(("s", "e"), [(27.0, 28.0), (28.1, 28.4), (28.6, 29.0), (29.0, 29.5)])
    def test_clean_window_near_damaged_frame_exact(self, fx_vbr, tmp_path, header, s, e):
        path = tmp_path / "damaged.flac"
        path.write_bytes(_damage_frame(fx_vbr.path.read_bytes(), int(28.5 * 48000), header=header))
        full, _ = ondio.read_flac(str(path), s, e, use_range=False)
        assert np.array_equal(full, fx_vbr.data[round(s * 48000):round(e * 48000)])
        ranged, _ = ondio.read_flac(str(path), s, e, use_range=True)
        assert np.array_equal(ranged, full)

    @pytest.mark.parametrize("header", [False, True], ids=["body", "header"])
    def test_window_over_damaged_frame_raises(self, fx_vbr, tmp_path, header):
        path = tmp_path / "damaged.flac"
        path.write_bytes(_damage_frame(fx_vbr.path.read_bytes(), int(28.5 * 48000), header=header))
        with pytest.raises(OndioError, match="use_range=False"):
            ondio.read_flac(str(path), 28.4, 28.6, use_range=True)
