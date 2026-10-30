"""FLAC metadata layout and frame location — parsing only, no decoding.

A ranged read fetches bytes from the middle of a stream, and the bitrate of
real recordings varies too much to know from a byte position which sample it
holds. Each frame header records its frame number (fixed-blocksize streams)
or first sample number (variable-blocksize streams), protected by a CRC-8.
find_frame_chain locates real frames in a fetched buffer, and rebuild_stream
wraps them in a minimal stream that libsndfile decodes when read sequentially.

Ported from brookisme's frame-chain fix:
https://gist.github.com/brookisme/d25aa5bafd2ed4fbfbce2f97f274317e
It was verified sample-exact against full-file seeks on real recordings.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

from ondio.backends.protocol import StorageBackend
from ondio.types import OndioError

# Marker of the beginning of a FLAC stream: ASCII "fLaC".
MARKER = b"fLaC"

# First fetch for header parsing; typically covers the fLaC marker,
# STREAMINFO, and the other metadata block headers.
_INITIAL_HEADER_BYTES = 4096

_STREAMINFO_LEN = 34
_SEEKTABLE_TYPE = 3
_SEEKPOINT_LEN = 18
_PLACEHOLDER = 0xFFFFFFFFFFFFFFFF

# Frame sync codes: fixed-blocksize streams number frames, variable-blocksize
# streams number samples.
_FIXED_SYNC = b"\xff\xf8"
_VARIABLE_SYNC = b"\xff\xf9"

# Longest frame header: sync (2) + codes (2) + coded number (7) + block
# size (2) + sample rate (2) + CRC-8 (1).
MAX_HEADER_BYTES = 16

# Frame header field codes (FLAC format spec, "Frame header").
_BLOCK_SIZES = {1: 192, 2: 576, 3: 1152, 4: 2304, 5: 4608, **{c: 256 << (c - 8) for c in range(8, 16)}}
_SAMPLE_RATES = {
    1: 88200, 2: 176400, 3: 192000, 4: 8000, 5: 16000, 6: 22050,
    7: 24000, 8: 32000, 9: 44100, 10: 48000, 11: 96000,
}
# Header bytes that follow the coded number for block-size codes 6-7 and
# sample-rate codes 12-14.
_BLOCK_SIZE_EXTRA_BYTES = {6: 1, 7: 2}
_SAMPLE_RATE_EXTRA_BYTES = {12: 1, 13: 2, 14: 2}
_SAMPLE_SIZES = {1: 8, 2: 12, 4: 16, 5: 20, 6: 24, 7: 32}
# UTF-8-style coded number: (lead mask, lead value, value mask, continuation bytes)
_UTF8_LEADS = (
    (0x80, 0x00, 0x7F, 0), (0xE0, 0xC0, 0x1F, 1), (0xF0, 0xE0, 0x0F, 2),
    (0xF8, 0xF0, 0x07, 3), (0xFC, 0xF8, 0x03, 4), (0xFE, 0xFC, 0x01, 5),
    (0xFF, 0xFE, 0x00, 6),
)


def _make_crc8_table() -> list[int]:
    """CRC-8, polynomial x^8 + x^2 + x + 1 (0x07), as FLAC frame headers use."""
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = ((c << 1) ^ 0x07) & 0xFF if c & 0x80 else (c << 1) & 0xFF
        table.append(c)
    return table


_CRC8_TABLE = _make_crc8_table()


def _crc8(data: bytes) -> int:
    c = 0
    for b in data:
        c = _CRC8_TABLE[c ^ b]
    return c


@dataclass(frozen=True)
class StreamLayout:
    """Stream facts needed to locate and decode frames."""

    streaminfo: bytes  # the raw 34-byte STREAMINFO body
    audio_offset: int  # absolute offset of the first frame
    sample_rate: int
    channels: int
    bits_per_sample: int
    total_samples: int
    min_blocksize: int
    max_blocksize: int
    max_framesize: int
    # (sample, byte offset relative to audio_offset), sorted, placeholders dropped
    seekpoints: tuple[tuple[int, int], ...]

    @property
    def frame_bytes_guess(self) -> int:
        """Estimated size of one frame in bytes.

        STREAMINFO's max_framesize when the encoder recorded it. Otherwise
        the raw PCM size of the largest block, with one more bit per sample
        for a stereo side channel, plus 64 bytes for headers. A frame larger
        than this estimate makes a ranged read raise; it never returns wrong
        samples.
        """
        if self.max_framesize:
            return self.max_framesize
        return self.max_blocksize * (self.channels * self.bits_per_sample + 1) // 8 + 64


@dataclass(frozen=True)
class Frame:
    """A located frame: absolute offset, first sample number, samples in it."""

    offset: int
    first_sample: int
    block_size: int

    @property
    def end_sample(self) -> int:
        """The sample number just past this frame."""
        return self.first_sample + self.block_size


def parse_layout(backend: StorageBackend, uri: str, *, seektable: bool) -> StreamLayout:
    """Walk the metadata blocks; fetch the SEEKTABLE body only when asked.

    Block headers are 4 bytes (bit 7 = last-block flag, bits 6-0 = type, then
    a 24-bit body length); other bodies are skipped without being fetched, so
    large embedded artwork is never fetched.

    STREAMINFO bytes 10-17 are one packed big-endian 64-bit field:

        bits 63-44  sample rate in Hz         (20 bits)
        bits 43-41  channels - 1               (3 bits)
        bits 40-36  bits-per-sample - 1        (5 bits)
        bits 35-0   total samples in stream   (36 bits; 0 = unknown)
    """
    buf = backend.read_range(uri, 0, _INITIAL_HEADER_BYTES - 1)

    def read(offset: int, n: int) -> bytes:
        if offset + n <= len(buf):
            return bytes(buf[offset:offset + n])
        return bytes(backend.read_range(uri, offset, offset + n - 1))

    # 4 + 4 + 34 = marker + STREAMINFO block header + STREAMINFO body: the
    # minimum for a parseable file.
    if len(buf) < 42 or buf[:4] != MARKER:
        raise OndioError(f"not a FLAC stream: {uri}")
    is_last = bool(buf[4] & 0x80)
    # The spec requires STREAMINFO (type 0, body exactly 34 bytes) first.
    if buf[4] & 0x7F != 0 or int.from_bytes(buf[5:8], "big") != _STREAMINFO_LEN:
        raise OndioError(
            f"invalid FLAC stream (STREAMINFO must be the first metadata block): {uri}"
        )
    streaminfo = bytes(buf[8:42])
    points: list[tuple[int, int]] = []
    pos = 42
    while not is_last:
        header = read(pos, 4)
        if len(header) < 4:
            raise OndioError(f"truncated FLAC metadata region: {uri}")
        is_last = bool(header[0] & 0x80)
        length = int.from_bytes(header[1:4], "big")
        if seektable and header[0] & 0x7F == _SEEKTABLE_TYPE:
            table = read(pos + 4, length)
            if len(table) < length:
                raise OndioError(f"truncated FLAC metadata region: {uri}")
            for k in range(0, length - _SEEKPOINT_LEN + 1, _SEEKPOINT_LEN):
                sample = int.from_bytes(table[k:k + 8], "big")
                if sample != _PLACEHOLDER:
                    points.append((sample, int.from_bytes(table[k + 8:k + 16], "big")))
        pos += 4 + length

    packed = int.from_bytes(streaminfo[10:18], "big")
    return StreamLayout(
        streaminfo=streaminfo,
        audio_offset=pos,
        sample_rate=packed >> 44,
        channels=((packed >> 41) & 0x7) + 1,
        bits_per_sample=((packed >> 36) & 0x1F) + 1,
        total_samples=packed & ((1 << 36) - 1),
        min_blocksize=int.from_bytes(streaminfo[0:2], "big"),
        max_blocksize=int.from_bytes(streaminfo[2:4], "big"),
        max_framesize=int.from_bytes(streaminfo[7:10], "big"),
        seekpoints=tuple(sorted(points)),
    )


def _parse_frame_header(buf: bytes, i: int, base: int, layout: StreamLayout) -> Frame | None:
    """The frame starting at buf[i] if a valid header is there, else None.

    Header fields are cross-checked against STREAMINFO and the CRC-8 must
    match; a random byte pattern still passes about once in a few thousand,
    which find_frame_chain's continuity check then rejects.
    """
    if i + 6 > len(buf) or buf[i] != 0xFF or (buf[i + 1] & 0xFE) != 0xF8:
        return None
    variable = buf[i + 1] & 0x01
    bs_code, sr_code = buf[i + 2] >> 4, buf[i + 2] & 0x0F
    ch_code, ss_code = buf[i + 3] >> 4, (buf[i + 3] >> 1) & 0x07
    if bs_code == 0 or sr_code == 0x0F or ch_code > 10 or ss_code == 3 or buf[i + 3] & 0x01:
        return None
    if (ch_code + 1 if ch_code < 8 else 2) != layout.channels:
        return None
    if ss_code and _SAMPLE_SIZES.get(ss_code) != layout.bits_per_sample:
        return None
    if sr_code in _SAMPLE_RATES and _SAMPLE_RATES[sr_code] != layout.sample_rate:
        return None
    lead = buf[i + 4]
    for mask, value, value_mask, extra in _UTF8_LEADS:
        if lead & mask == value:
            number = lead & value_mask
            break
    else:
        return None
    j = i + 5
    # The CRC-8 byte follows the continuation bytes and the explicit block
    # size and sample rate fields, if the codes call for them.
    crc_at = j + extra + _BLOCK_SIZE_EXTRA_BYTES.get(bs_code, 0) + _SAMPLE_RATE_EXTRA_BYTES.get(sr_code, 0)
    if crc_at >= len(buf):
        return None
    for cont in buf[j:j + extra]:
        if cont & 0xC0 != 0x80:
            return None
        number = (number << 6) | (cont & 0x3F)
    j += extra
    if bs_code == 6:
        block_size = buf[j] + 1
    elif bs_code == 7:
        block_size = int.from_bytes(buf[j:j + 2], "big") + 1
    else:
        block_size = _BLOCK_SIZES[bs_code]
    if _crc8(buf[i:crc_at]) != buf[crc_at]:
        return None
    # Fixed-blocksize streams number frames, and every frame but the last
    # holds max_blocksize samples.
    first_sample = number if variable else number * layout.max_blocksize
    if layout.total_samples and first_sample + block_size > layout.total_samples:
        return None
    return Frame(base + i, first_sample, block_size)


def scan_frames(
    buf: bytes, base: int, layout: StreamLayout, start: int = 0, stop: int | None = None
) -> list[Frame]:
    """Frames whose valid headers start in buf[start:stop], by offset.

    `base` is the absolute offset of buf[0]. A header may run past `stop`;
    it is parsed from the whole buffer.
    """
    stop = len(buf) if stop is None else min(stop, len(buf))
    frames = []
    for sync in (_FIXED_SYNC, _VARIABLE_SYNC):
        i = buf.find(sync, start, stop + 1)
        while i != -1 and i < stop:
            frame = _parse_frame_header(buf, i, base, layout)
            if frame:
                frames.append(frame)
            i = buf.find(sync, i + 1, stop + 1)
    frames.sort(key=lambda f: f.offset)
    return frames


def find_frame_chain(
    buf: bytes,
    base: int,
    layout: StreamLayout,
    *,
    cover: int | None = None,
    candidates: list[Frame] | None = None,
) -> list[Frame]:
    """A run of frames in buf, each continued by the next.

    `base` is the absolute offset of buf[0]. A false sync that passes the
    header checks almost never forms a chain by itself. It can join a real
    chain: as its first frame, or, very rarely, in place of a real frame
    with the same sample number. Either way the decode then fails (the
    CRC-16 check, or a short sample count), so a false frame causes an
    error, never wrong samples.

    Damage splits the frames into several runs. With `cover`, the longest
    run of two or more frames that holds sample `cover` is chosen; otherwise,
    or if none holds it, the longest run. `candidates`, if given, is
    scan_frames' result for buf, kept by a caller that grows buf.
    """
    if candidates is None:
        candidates = scan_frames(buf, base, layout)
    by_first_sample: dict[int, list[Frame]] = {}
    for frame in candidates:
        by_first_sample.setdefault(frame.first_sample, []).append(frame)

    best: list[Frame] = []
    best_covering: list[Frame] = []
    used: set[Frame] = set()
    for start in candidates:
        if start in used:
            continue
        chain = [start]
        while True:
            nxt = next(
                (f for f in by_first_sample.get(chain[-1].end_sample, ()) if f.offset > chain[-1].offset),
                None,
            )
            if nxt is None:
                break
            chain.append(nxt)
        used.update(chain)
        if len(chain) > len(best):
            best = chain
        if (
            cover is not None
            and len(chain) > max(1, len(best_covering))
            and chain[0].first_sample <= cover < chain[-1].end_sample
        ):
            best_covering = chain
    return best_covering or best


def sample_to_byte(layout: StreamLayout, file_size: int, sample: int) -> int:
    """Estimated absolute byte offset of `sample`.

    Interpolates between seek points, anchored at (0, 0) and the end of the
    file; with no seek table this is the stream's average bitrate. Seek points
    at or past the end of the file are ignored: a file truncated after its
    seek table was written has them.
    """
    total = layout.total_samples
    audio_bytes = file_size - layout.audio_offset
    points = [
        (0, 0),
        *(p for p in layout.seekpoints if 0 < p[0] < total and p[1] < audio_bytes),
        (total, audio_bytes),
    ]
    sample = min(max(sample, 0), total)
    k = max(0, bisect.bisect_right([p[0] for p in points], sample) - 1)
    k = min(k, len(points) - 2)
    (s0, b0), (s1, b1) = points[k], points[k + 1]
    frac = (sample - s0) / (s1 - s0) if s1 > s0 else 0.0
    return layout.audio_offset + int(b0 + frac * (b1 - b0))


def rebuild_stream(layout: StreamLayout, frames: bytes, n_samples: int) -> bytes:
    """A minimal FLAC stream: marker, STREAMINFO, then `frames`.

    STREAMINFO is the last metadata block, with total_samples set to
    `n_samples` and the MD5 zeroed. The frames keep their original numbers,
    so the result must be read sequentially: a seek (including the one
    soundfile.read() makes) fails.
    """
    si = bytearray(layout.streaminfo)
    packed = int.from_bytes(si[10:18], "big")
    packed = (packed & ~((1 << 36) - 1)) | n_samples
    si[10:18] = packed.to_bytes(8, "big")
    si[18:34] = bytes(16)
    return MARKER + bytes([0x80]) + _STREAMINFO_LEN.to_bytes(3, "big") + bytes(si) + frames
