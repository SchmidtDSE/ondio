"""Frame layer: metadata layout, frame-chain location, byte estimates and
stream rebuilding (ondio._flac_frames). No decoding happens in the module
itself; the tests decode rebuilt streams with soundfile to prove the located
frames are real audio at the claimed sample positions."""

import io
from itertools import pairwise

import numpy as np
import pytest
import soundfile as sf
from hypothesis import HealthCheck, given, settings, strategies as st

from ondio import OndioError
from ondio import flac as flac_mod
from ondio._flac_frames import (
    StreamLayout,
    find_frame_chain,
    parse_layout,
    rebuild_stream,
    sample_to_byte,
    scan_frames,
)

from flac_helpers import BytesBackend, build_metadata_region, build_streaminfo

_PLACEHOLDER = 0xFFFFFFFFFFFFFFFF


def _seekpoint(sample, offset, n=4096):
    return sample.to_bytes(8, "big") + offset.to_bytes(8, "big") + n.to_bytes(2, "big")


def _layout(fx):
    return parse_layout(BytesBackend(fx.path.read_bytes()), fx.uri, seektable=True)


def _synthetic_layout(**fields):
    """A 48 kHz mono 16-bit layout with no seek table; `fields` override."""
    defaults = dict(
        streaminfo=bytes(34),
        audio_offset=100,
        sample_rate=48000,
        channels=1,
        bits_per_sample=16,
        total_samples=3000,
        min_blocksize=1000,
        max_blocksize=1000,
        max_framesize=0,
        seekpoints=(),
    )
    return StreamLayout(**{**defaults, **fields})


def _crc8(data):
    """CRC-8, polynomial 0x07, computed bit by bit (independent of the module's table)."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _coded_number(n):
    """FLAC's UTF-8-style coding of a frame or sample number (up to 36 bits)."""
    if n < 0x80:
        return bytes([n])
    for k in range(1, 7):  # k continuation bytes carry 6 bits each
        if n < 1 << (6 + 5 * k):
            lead = ((0xFF << (7 - k)) & 0xFF) | (n >> (6 * k))
            return bytes([lead]) + bytes(0x80 | (n >> (6 * i)) & 0x3F for i in range(k - 1, -1, -1))
    raise ValueError(n)


def _variable_header(first_sample, block_size, *, sample_rate_hz=None):
    """A mono 16-bit variable-blocksize frame header with an explicit 16-bit
    block size; with `sample_rate_hz`, also an explicit 16-bit sample rate
    (the longest header for a given coded number)."""
    sr_code = 13 if sample_rate_hz else 0
    head = bytes([0xFF, 0xF9, (7 << 4) | sr_code, 4 << 1]) + _coded_number(first_sample)
    head += (block_size - 1).to_bytes(2, "big")
    if sample_rate_hz:
        head += sample_rate_hz.to_bytes(2, "big")
    return head + bytes([_crc8(head)])


class TestParseLayout:
    def test_matches_extract_flac_header(self, fx):
        layout = _layout(fx)
        header = flac_mod.extract_flac_header(BytesBackend(fx.path.read_bytes()), fx.uri)
        assert layout.sample_rate == header.sample_rate
        assert layout.channels == header.channels
        assert layout.bits_per_sample == header.bits_per_sample
        assert layout.total_samples == header.total_samples
        assert layout.audio_offset == header.audio_data_offset
        assert layout.max_blocksize > 0

    def test_seektable_parsed_and_placeholders_dropped(self):
        streaminfo = build_streaminfo(sample_rate=48000, channels=1, bits_per_sample=16, total_samples=10_000)
        body = _seekpoint(4096, 9000) + _seekpoint(0, 0) + _seekpoint(_PLACEHOLDER, 0)
        # a padding block after the seek table: bodies of other types are skipped
        region = build_metadata_region(streaminfo, [(3, body), (1, bytes(5000))])
        layout = parse_layout(BytesBackend(region), "mem://a.flac", seektable=True)
        assert layout.seekpoints == ((0, 0), (4096, 9000))
        assert layout.audio_offset == len(region)
        assert parse_layout(BytesBackend(region), "mem://a.flac", seektable=False).seekpoints == ()

    def test_not_flac_raises(self):
        with pytest.raises(OndioError, match="not a FLAC stream"):
            parse_layout(BytesBackend(b"RIFF" + bytes(100)), "mem://a.wav", seektable=True)

    def test_streaminfo_not_first_raises(self):
        streaminfo = build_streaminfo(sample_rate=44100, channels=2, bits_per_sample=16, total_samples=100)
        region = build_metadata_region(streaminfo, [(1, bytes(10))])
        # swap: padding block first
        broken = b"fLaC" + bytes([0x01]) + (10).to_bytes(3, "big") + bytes(10) + region[8:]
        with pytest.raises(OndioError, match="STREAMINFO"):
            parse_layout(BytesBackend(broken), "mem://a.flac", seektable=True)

    def test_truncated_metadata_region_raises(self):
        streaminfo = build_streaminfo(sample_rate=44100, channels=2, bits_per_sample=16, total_samples=100)
        # STREAMINFO's header says more blocks follow, but the stream ends
        region = b"fLaC" + bytes([0x00]) + (34).to_bytes(3, "big") + streaminfo
        with pytest.raises(OndioError, match="truncated"):
            parse_layout(BytesBackend(region), "mem://a.flac", seektable=True)


class TestFrameChain:
    @settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(cut=st.floats(0.01, 0.95))
    def test_chain_from_arbitrary_offset_is_real_audio(self, fx, cut):
        raw = fx.path.read_bytes()
        layout = _layout(fx)
        k = int(cut * len(raw))
        buf = raw[k:]
        chain = find_frame_chain(buf, k, layout)
        assert len(chain) >= 2
        assert chain[0].first_sample % layout.max_blocksize == 0
        assert all(a.end_sample == b.first_sample and a.offset < b.offset for a, b in pairwise(chain))
        c0, cn = chain[0], chain[-1]
        stream = rebuild_stream(layout, buf[c0.offset - k:cn.offset - k], cn.first_sample - c0.first_sample)
        with sf.SoundFile(io.BytesIO(stream)) as f:
            decoded = f.read(dtype="float32", always_2d=True)
        assert np.array_equal(decoded, fx.data[c0.first_sample:cn.first_sample])

    def test_random_bytes_yield_no_chain(self, fx):
        buf = bytearray(np.random.default_rng(0).bytes(1_000_000))
        for i in range(0, len(buf) - 1, 997):
            buf[i:i + 2] = b"\xff\xf8"
        assert len(find_frame_chain(bytes(buf), 0, _layout(fx))) <= 1


class TestVariableBlocksize:
    """Variable-blocksize streams number samples, not frames. soundfile cannot
    write them, so these tests build frame headers by hand."""

    def test_chain_from_sample_numbers(self):
        # 5e9 needs the longest (7-byte) coded number
        sizes = [1000, 700, 1300]
        starts = [5_000_000_000, 5_000_001_000, 5_000_001_700]
        buf = b""
        for start, size in zip(starts, sizes):
            buf += bytes(50) + _variable_header(start, size)
        layout = _synthetic_layout(total_samples=0, min_blocksize=700, max_blocksize=1300)
        chain = find_frame_chain(buf, 1000, layout)
        assert [f.first_sample for f in chain] == starts
        assert [f.block_size for f in chain] == sizes
        assert chain[-1].end_sample == 5_000_003_000
        assert chain[0].offset == 1050

    @pytest.mark.parametrize("sample_rate_hz", [None, 48000], ids=["short", "longest"])
    def test_header_parses_at_buffer_end(self, sample_rate_hz):
        # The longest header (16 bytes) has an explicit block size and sample
        # rate: 4 bytes after the coded number.
        header = _variable_header(5_000_000_000, 1000, sample_rate_hz=sample_rate_hz)
        assert len(header) == (16 if sample_rate_hz else 14)
        layout = _synthetic_layout(total_samples=0)
        buf = bytes(20) + header
        assert [f.offset for f in scan_frames(buf, 0, layout)] == [20]
        assert scan_frames(buf[:-1], 0, layout) == []


class TestSampleToByte:
    def test_exact_at_seekpoints_and_monotonic(self):
        layout = _synthetic_layout(seekpoints=((0, 0), (1000, 5000), (2000, 6000)))
        assert sample_to_byte(layout, 20100, 1000) == 5100
        assert sample_to_byte(layout, 20100, 1500) == 5600
        offsets = [sample_to_byte(layout, 20100, s) for s in range(0, 3001, 7)]
        assert offsets == sorted(offsets)
        assert layout.frame_bytes_guess == 1000 * (1 * 16 + 1) // 8 + 64

    def test_seekpoints_past_end_of_file_ignored(self):
        # a file truncated after its seek table was written
        layout = _synthetic_layout(seekpoints=((1000, 5000), (2000, 90_000)))
        offsets = [sample_to_byte(layout, 20_100, s) for s in range(0, 3001, 7)]
        assert all(o <= 20_100 for o in offsets)
        assert offsets == sorted(offsets)


def test_scan_frames_window_matches_full_scan(fx):
    raw = fx.path.read_bytes()
    layout = _layout(fx)
    full = scan_frames(raw, 0, layout)
    a, b = len(raw) // 3, 2 * len(raw) // 3
    part = scan_frames(raw, 0, layout, a, b)
    assert part == [f for f in full if a <= f.offset < b]
    assert find_frame_chain(raw, 0, layout) == find_frame_chain(raw, 0, layout, candidates=full)
