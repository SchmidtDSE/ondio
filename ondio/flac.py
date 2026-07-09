"""FLAC header parsing and pydub/ffmpeg-based reads.

Public surface (each mirrored URI-first in the dispatcher):

* extract_flac_header — parse STREAMINFO fetching only header bytes;
* read_flac — the whole stream or a [start_sec, end_sec] window, in memory:
  decoded PCM as a soundfile-style numpy array (default), or with
  decode=False the bytes of a standalone FLAC file;
* download_flac — the same window semantics, written to a local .flac path
  (the counterpart of legacy soundhub_utils io.read_flac(src, dest, ...)).

Calls that need no decoding — read_flac(decode=False) or download_flac with
no window — never touch ffmpeg: they are plain byte copies and preserve the
original file bytes exactly. Everything else goes through pydub -> ffmpeg,
and a windowed FLAC result is a fresh encode: losslessly identical PCM, but
not byte-identical to any slice of the source (new frame boundaries, new
STREAMINFO, no original tags).

What changed versus soundhub_utils:

* the STREAMINFO bit arithmetic is fixed — legacy read bits_per_sample from
  the wrong bits (reporting e.g. 17 for a 16-bit file) and read the channel
  count from total-samples bits (reporting 1 for nearly any file);
* backend-agnostic: every function takes (backend, uri) and works on any
  StorageBackend;
* the four legacy entry points collapse to read_flac/download_flac, with the
  window optional and the fetch strategy overridable via use_range.
* the byte-range path hardens the soundhub_utils estimate: it interpolates over 
  the audio-data region only (legacy measured from byte 0, so a short window
  near t=0 could land entirely inside a large metadata region and fail to
  decode). It also enforces a minimum padding around the window, because 
  proportional padding on very short clips can fail.

Requirements: pydub + audioop-lts (Python >= 3.13 removed stdlib audioop) and
the ffmpeg CLI on PATH — pydub shells out to it for all decoding/encoding.

Caveats inherited from the soundhub_utils design (kept for parity, documented here):

* slicing is at integer-millisecond granularity, not sample-exact;
* the byte-range path estimates positions linearly from the header's
  duration and hands ffmpeg a headerless mid-stream blob; ffmpeg starts
  decoding at the first frame boundary it finds, so the window is aligned
  to where the estimate landed, not to exact samples — and if STREAMINFO
  lies about the duration (truncated recorder files exist), the returned audio
  is silently from the wrong position. When in doubt, pass use_range=False and
  pay for the full download.
"""

from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Literal, overload

from ondio.backends.protocol import StorageBackend
from ondio.types import FlacHeader, OndioError

if TYPE_CHECKING:
    import numpy as np

# Marker of the beginning of a FLAC stream: ASCII "fLaC".
_MARKER = b"fLaC"

# First fetch for header parsing; typically covers the fLaC marker,
# STREAMINFO, and the other metadata block headers.
_INITIAL_HEADER_BYTES = 4096

# Heuristic for when a ranged download pays off: big file AND a small
# window. Otherwise the whole file is downloaded and sliced, which is exact.
_RANGE_MIN_FILE_BYTES = 50 * 1024 * 1024
_RANGE_MAX_WINDOW_RATIO = 0.33

# Minimum padding (seconds) fetched around a ranged window. Proportional
# padding gives tiny windows almost no slack, and slack is what absorbs the
# byte estimate's error; the floor guarantees real slack at negligible cost.
_RANGE_MIN_PAD_SEC = 0.5



def _require_pydub():
    try:
        from pydub import AudioSegment
    except ImportError as exc:
        raise ImportError(
            "FLAC decoding requires the 'audio' extra (pydub + audioop-lts):"
            " pip install ondio[audio] — and the ffmpeg CLI must be on PATH"
        ) from exc
    return AudioSegment


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------


def _read_streaminfo(backend: StorageBackend, uri: str) -> tuple[bytes, int]:
    """The 34-byte STREAMINFO body and the audio-data offset.

    Walks the metadata block headers (4 bytes each: bit 7 = last-block flag,
    bits 6-0 = type, then a 24-bit body length) without reading block bodies,
    so megabytes of embedded artwork cost nothing.
    """
    buf = backend.read_range(uri, 0, _INITIAL_HEADER_BYTES - 1)
    # 4 + 4 + 34 = marker + STREAMINFO block header + STREAMINFO body: the
    # minimum for a parseable file.
    if len(buf) < 42 or buf[:4] != _MARKER:
        raise OndioError(f"not a FLAC stream: {uri}")
    is_last = bool(buf[4] & 0x80)
    # The spec requires STREAMINFO (type 0, body exactly 34 bytes) first.
    if buf[4] & 0x7F != 0 or int.from_bytes(buf[5:8], "big") != 34:
        raise OndioError(
            f"invalid FLAC stream (STREAMINFO must be the first metadata block): {uri}"
        )
    streaminfo = bytes(buf[8:42])
    pos = 42
    while not is_last:
        header = (
            bytes(buf[pos:pos + 4])
            if pos + 4 <= len(buf)
            else backend.read_range(uri, pos, pos + 3)
        )
        if len(header) < 4:
            raise OndioError(f"truncated FLAC metadata region: {uri}")
        is_last = bool(header[0] & 0x80)
        pos += 4 + int.from_bytes(header[1:4], "big")
    return streaminfo, pos


def extract_flac_header(backend: StorageBackend, uri: str) -> FlacHeader:
    """Parse STREAMINFO fetching only header bytes.

    Metadata block bodies are skipped, not downloaded.

    STREAMINFO bytes 10-17 are one packed big-endian 64-bit field:

        bits 63-44  sample rate in Hz         (20 bits)
        bits 43-41  channels - 1               (3 bits)
        bits 40-36  bits-per-sample - 1        (5 bits)
        bits 35-0   total samples in stream   (36 bits; 0 = unknown)

    (This is the fix over soundhub_utils, which read bits_per_sample from
    bits 36-32 plus a stray total-samples bit, and channels from bits 35-33 —
    total-samples bits — instead of 43-41.)

    Args:
        backend: The storage backend to fetch through.
        uri: URI of a FLAC file, addressable by `backend`.

    Returns:
        The parsed [`FlacHeader`][ondio.types.FlacHeader].

    Raises:
        OndioError: If `uri` is not a valid FLAC stream (missing marker,
            malformed metadata region, or STREAMINFO not first).
    """
    streaminfo, audio_data_offset = _read_streaminfo(backend, uri)
    packed = int.from_bytes(streaminfo[10:18], "big")
    return FlacHeader(
        sample_rate=packed >> 44,
        channels=((packed >> 41) & 0x7) + 1,
        bits_per_sample=((packed >> 36) & 0x1F) + 1,
        total_samples=packed & ((1 << 36) - 1),
        md5=streaminfo[18:34],
        audio_data_offset=audio_data_offset,
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _decode_flac(data: bytes):
    """Decode FLAC bytes to an AudioSegment via a temp file.

    A temp file rather than a pipe mirrors soundhub_utils: ffmpeg gets a
    seekable input, which is more reliable across its demuxers. For range
    downloads `data` has no fLaC header at all — ffmpeg scans for frame sync
    and decodes from the first complete frame it finds.
    """
    AudioSegment = _require_pydub()
    with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as tmp:
        tmp.write(data)
        path = tmp.name
    try:
        return AudioSegment.from_file(path, format="flac")
    finally:
        os.unlink(path)


def _estimate_byte_range(
    file_size: int,
    audio_data_offset: int,
    total_duration: float,
    start_sec: float,
    end_sec: float,
    padding_ratio: float,
) -> tuple[int, int]:
    """Linear seconds->bytes estimate, padded by `padding_ratio` of the window
    duration on each side.

    Interpolating from audio_data_offset (soundhub_utils measured from byte 0)
    keeps metadata out of the rate and guarantees no window can land inside
    the metadata region — recorder files carry seek tables large enough to
    swallow the whole estimated range for a short window near t=0.
    """
    padding_sec = max(padding_ratio * (end_sec - start_sec), _RANGE_MIN_PAD_SEC)
    padded_start = max(0.0, start_sec - padding_sec)
    padded_end = min(total_duration, end_sec + padding_sec)
    bytes_per_second = (file_size - audio_data_offset) / total_duration
    start_byte = audio_data_offset + int(padded_start * bytes_per_second)
    end_byte = min(file_size - 1, audio_data_offset + int(padded_end * bytes_per_second))
    return start_byte, end_byte


def _read_window(
    backend: StorageBackend,
    uri: str,
    start_sec: float | None,
    end_sec: float | None,
    *,
    padding_ratio: float,
    use_range: bool | None,
):
    """Shared core: fetch, decode, and millisecond-slice the requested window.

    start_sec=None means 0; end_sec=None means the end of the stream. Returns
    the pydub AudioSegment for [start_sec, end_sec] (end clamped to the
    stream duration).
    """
    # Caller errors are ValueError; stream/IO problems are OndioError.
    start_sec = 0.0 if start_sec is None else start_sec
    if start_sec < 0:
        raise ValueError(f"start_sec must be >= 0, got {start_sec}")
    if end_sec is not None and end_sec <= start_sec:
        raise ValueError(f"end_sec ({end_sec}) must be > start_sec ({start_sec})")
    if padding_ratio < 0:
        raise ValueError(f"padding_ratio must be >= 0, got {padding_ratio}")

    header = extract_flac_header(backend, uri)
    duration = header.duration
    # The spec allows total_samples = 0 ("unknown length"); both the window
    # validation and the byte estimate need a real duration.
    if duration <= 0:
        raise OndioError(f"FLAC stream reports zero duration: {uri}")
    if start_sec >= duration:
        raise ValueError(f"start_sec ({start_sec}) is past the end of the stream ({duration:.3f}s)")
    # Clamp, like Python slices past the end.
    end_sec = duration if end_sec is None else min(end_sec, duration)

    file_size = backend.size(uri)
    if use_range is None:
        use_range = (
            file_size > _RANGE_MIN_FILE_BYTES
            and (end_sec - start_sec) / duration < _RANGE_MAX_WINDOW_RATIO
        )

    if use_range:
        start_byte, end_byte = _estimate_byte_range(
            file_size, header.audio_data_offset, duration, start_sec, end_sec, padding_ratio
        )
        audio = _decode_flac(backend.read_range(uri, start_byte, end_byte))
        # The decoded audio is assumed to begin at the padded start time; the
        # window is cut relative to that. Estimate error shifts the window.
        padded_start = max(0.0, start_sec - (end_sec - start_sec) * padding_ratio)
        rel_ms = int((start_sec - padded_start) * 1000)
        return audio[rel_ms:rel_ms + int((end_sec - start_sec) * 1000)]

    audio = _decode_flac(backend.read(uri))
    return audio[int(start_sec * 1000):int(end_sec * 1000)]


def _segment_to_array(chunk) -> tuple[np.ndarray, int]:
    """(samples, sample_rate): float32 of shape (frames, channels), normalized
    to [-1, 1] the same way libsndfile/soundfile normalizes integer PCM.
    """
    import numpy as np  # the 'audio' extra guarantees numpy

    width = chunk.sample_width
    if width == 3:
        # No 3-byte numpy dtype: assemble little-endian int24 by hand, then
        # sign-extend from bit 23.
        b = np.frombuffer(chunk.raw_data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        values = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        values -= (values & 0x800000) << 1
    else:
        dtype = {1: np.int8, 2: np.int16, 4: np.int32}[width]
        values = np.frombuffer(chunk.raw_data, dtype=dtype).astype(np.int32)
    samples = values.reshape(-1, chunk.channels).astype(np.float32) / float(1 << (8 * width - 1))
    return samples, chunk.frame_rate


@overload
def read_flac(
    backend: StorageBackend,
    uri: str,
    start_sec: float | None = None,
    end_sec: float | None = None,
    *,
    decode: Literal[True] = True,
    padding_ratio: float = 0.25,
    use_range: bool | None = None,
) -> tuple[np.ndarray, int]: ...


@overload
def read_flac(
    backend: StorageBackend,
    uri: str,
    start_sec: float | None = None,
    end_sec: float | None = None,
    *,
    decode: Literal[False],
    padding_ratio: float = 0.25,
    use_range: bool | None = None,
) -> bytes: ...


def read_flac(
    backend: StorageBackend,
    uri: str,
    start_sec: float | None = None,
    end_sec: float | None = None,
    *,
    decode: bool = True,
    padding_ratio: float = 0.25,
    use_range: bool | None = None,
) -> tuple[np.ndarray, int] | bytes:
    """Read a FLAC file — whole, or a `[start_sec, end_sec]` window — into memory.

    Decoding requires the `audio` extra (pydub + audioop-lts + numpy) and the
    ffmpeg CLI on PATH; the whole-file `decode=False` fast path needs neither.

    Args:
        backend: The storage backend to fetch through.
        uri: URI of a FLAC file, addressable by `backend`.
        start_sec: Window start in seconds; None means from the beginning.
        end_sec: Window end in seconds; None means to the end of the stream
            (values past EOF are clamped, like Python slices).
        decode: If True (default), return decoded PCM; if False, return the
            bytes of a standalone FLAC file — the original bytes exactly when
            no window is given (a plain read, no ffmpeg), a fresh lossless
            encode of the slice otherwise.
        padding_ratio: Fraction of the window duration fetched as extra
            padding on each side of the ranged download (ignored on the
            full-download path).
        use_range: Fetch strategy. None (default) picks per the legacy
            heuristic: ranged download for files > 50 MB when the window is
            < 1/3 of the stream, full download otherwise. True/False forces
            one path. Full download is exact; the ranged path is an estimate.

    Returns:
        With `decode=True`, `(samples, sample_rate)` where `samples` is a
        float32 array of shape `(frames, channels)` normalized to `[-1, 1]`
        like libsndfile/soundfile. With `decode=False`, FLAC bytes.

    Raises:
        ValueError: If the window is empty, negative, or starts past EOF.
        OndioError: If `uri` is not a valid FLAC stream, or the stream
            reports zero duration and a window was requested.
        ImportError: If decoding is required and the `audio` extra is missing.

    Warning:
        The ranged path estimates byte positions linearly from the header's
        duration: the slice is aligned only approximately (tens of ms), and a
        header that lies about the duration (truncated recorder files exist)
        yields audio from the wrong position with no error. Pass
        `use_range=False` when the header cannot be trusted.
    """
    whole = start_sec is None and end_sec is None
    if whole and not decode:
        return backend.read(uri)
    if whole:
        # No header fetch needed — also tolerates unknown-length streams.
        chunk = _decode_flac(backend.read(uri))
    else:
        chunk = _read_window(
            backend, uri, start_sec, end_sec,
            padding_ratio=padding_ratio, use_range=use_range,
        )
    if decode:
        return _segment_to_array(chunk)
    buf = io.BytesIO()
    chunk.export(buf, format="flac")
    return buf.getvalue()


def download_flac(
    backend: StorageBackend,
    uri: str,
    out_path: str | os.PathLike[str],
    start_sec: float | None = None,
    end_sec: float | None = None,
    *,
    padding_ratio: float = 0.25,
    use_range: bool | None = None,
) -> None:
    """Write a .flac file containing `[start_sec, end_sec]` of `uri` to `out_path`.

    With no window this is a byte-for-byte download of the original file (no
    ffmpeg). With one, the window is decoded, sliced at millisecond
    granularity, and re-encoded — losslessly identical PCM, but not
    byte-identical to the source. Window and fetch semantics, requirements,
    and the ranged-path warning are as in [`read_flac`][ondio.flac.read_flac].

    (Counterpart of legacy soundhub_utils `io.read_flac(src, dest, ...)`.)

    Args:
        backend: The storage backend to fetch through.
        uri: URI of a FLAC file, addressable by `backend`.
        out_path: Local `.flac` destination path; parent directories are created.
        start_sec: Window start in seconds; None means from the beginning.
        end_sec: Window end in seconds; None means to the end of the stream.
        padding_ratio: As in [`read_flac`][ondio.flac.read_flac].
        use_range: As in [`read_flac`][ondio.flac.read_flac].

    Raises:
        ValueError: If the window is empty, negative, or starts past EOF.
        OndioError: If `uri` is not a valid FLAC stream, or the stream
            reports zero duration and a window was requested.
        ImportError: If a window is given and the `audio` extra is missing.
    """
    if start_sec is None and end_sec is None:
        backend.download(uri, out_path)
        return
    chunk = _read_window(
        backend, uri, start_sec, end_sec,
        padding_ratio=padding_ratio, use_range=use_range,
    )
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    chunk.export(str(out), format="flac")
