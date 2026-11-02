"""FLAC header parsing and `soundfile`-based reads.

Public surface (each mirrored URI-first in the dispatcher):

* `extract_flac_header` — parse STREAMINFO fetching only header bytes;
* `read_flac` — the whole stream or a `[start_sec, end_sec]` window, in
  memory: decoded PCM as a `soundfile`-style numpy array (default), or with
  `decode=False` the bytes of a standalone FLAC file;
* `download_flac` — the same window semantics, written to a local `.flac`
  path (the counterpart of legacy `soundhub_utils`
  `io.read_flac(src, dest, ...)`).

Calls that need no decoding — `read_flac(decode=False)` or `download_flac`
with no window — are plain byte copies and preserve the original file bytes
exactly. Everything else decodes with `soundfile` (libsndfile, bundled in its
wheel; nothing shells out). Windows are cut by sample index —
`[round(start_sec * sr), round(end_sec * sr))` — and a windowed FLAC result
is a fresh encode: losslessly identical PCM, but not byte-identical to any
slice of the source (new frame boundaries, new STREAMINFO, no original tags).

Damaged input raises `OndioError`. A window that reaches into a corrupt or
truncated region raises; it never returns short or substitute audio. A
whole-file decode of a truncated or unknown-length stream also raises.

What changed versus `soundhub_utils`:

* the STREAMINFO bit arithmetic is fixed — legacy read `bits_per_sample`
  from the wrong bits (reporting e.g. 17 for a 16-bit file) and read the
  channel count from total-samples bits (reporting 1 for nearly any file);
* backend-agnostic: every function takes `(backend, uri)` and works on any
  `StorageBackend`;
* the four legacy entry points collapse to `read_flac`/`download_flac`, with
  the window optional and the fetch strategy overridable via `use_range`;
* windows are sample-exact; legacy sliced at integer milliseconds.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, overload

import numpy as np
import soundfile as sf

from ondio._flac_frames import (
    MARKER,
    MAX_HEADER_BYTES,
    Frame,
    StreamLayout,
    find_frame_chain,
    parse_layout,
    rebuild_stream,
    sample_to_byte,
    scan_frames,
)
from ondio._log import printer
from ondio.backends.protocol import StorageBackend
from ondio.types import FlacHeader, OndioError

# Heuristic for when a ranged download pays off: big file AND a small
# window. Otherwise the whole file is downloaded and sliced, which is exact.
_RANGE_MIN_FILE_BYTES = 50 * 1024 * 1024
_RANGE_MAX_WINDOW_RATIO = 0.33

# soundfile reports an unknown-length stream (STREAMINFO total_samples = 0)
# as a huge frame count.
_UNKNOWN_FRAMES = 2**62

# libsndfile writes FLAC at these depths only.
_FLAC_SUBTYPES = {8: "PCM_S8", 16: "PCM_16", 24: "PCM_24"}


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------


def extract_flac_header(backend: StorageBackend, uri: str) -> FlacHeader:
    """Parse STREAMINFO fetching only header bytes.

    Metadata block bodies are skipped, not downloaded.

    STREAMINFO bytes 10-17 are one packed big-endian 64-bit field:

        bits 63-44  sample rate in Hz         (20 bits)
        bits 43-41  channels - 1               (3 bits)
        bits 40-36  bits-per-sample - 1        (5 bits)
        bits 35-0   total samples in stream   (36 bits; 0 = unknown)

    (This is the fix over `soundhub_utils`, which read `bits_per_sample` from
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
    layout = parse_layout(backend, uri, seektable=False)
    return FlacHeader(
        sample_rate=layout.sample_rate,
        channels=layout.channels,
        bits_per_sample=layout.bits_per_sample,
        total_samples=layout.total_samples,
        md5=layout.streaminfo[18:34],
        audio_data_offset=layout.audio_offset,
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _decode_error(uri: str, reason: str, *, ranged: bool) -> OndioError:
    """The one message for a stream that won't decode as requested."""
    message = f"could not decode the requested audio from {uri}: {reason.rstrip('.')}."
    if ranged:
        message += (
            " The ranged fetch did not give the window's frames. A full"
            " download reads all audio that decodes: pass use_range=False."
        )
    else:
        message += " The stream is damaged (corrupt or truncated) where it was read."
    return OndioError(message)


def _decode_whole(uri: str, data: bytes) -> tuple[np.ndarray, int]:
    """Every sample of a complete FLAC stream, as soundfile's float32.

    Decoded straight to float32: no int32 or float64 copy of a stream that
    can run to gigabytes.
    """
    # libsndfile would otherwise decode any format it recognises.
    if data[:4] != MARKER:
        raise OndioError(f"not a FLAC stream: {uri}")
    try:
        with sf.SoundFile(io.BytesIO(data)) as f:
            if f.frames >= _UNKNOWN_FRAMES:
                raise OndioError(f"FLAC stream does not record its length; cannot decode it: {uri}")
            samples = f.read(dtype="float32", always_2d=True)
            expected, sample_rate = f.frames, f.samplerate
    except sf.SoundFileError as exc:
        raise _decode_error(uri, str(exc), ranged=False) from exc
    if len(samples) != expected:
        raise _decode_error(uri, f"decoded {len(samples)} of {expected} samples", ranged=False)
    return samples, sample_rate


def _decode_span(uri: str, data: bytes, first: int, stop: int) -> np.ndarray:
    """Samples [first, stop) of a complete FLAC stream, by seeking."""
    try:
        with sf.SoundFile(io.BytesIO(data)) as f:
            f.seek(first)
            samples = f.read(stop - first, dtype="int32", always_2d=True)
    except sf.SoundFileError as exc:
        raise _decode_error(uri, str(exc), ranged=False) from exc
    if len(samples) != stop - first:
        raise _decode_error(uri, f"decoded {len(samples)} of {stop - first} samples", ranged=False)
    return samples


def _to_float32(samples: np.ndarray) -> np.ndarray:
    """soundfile's float32 normalisation of its left-justified int32 PCM.

    Exact for depths up to 24 bits: scaling by a power of two only shifts
    the float32 exponent.
    """
    out = samples.astype(np.float32)
    out *= np.float32(2.0**-31)
    return out


def _encode_flac(samples: np.ndarray, sample_rate: int, bits_per_sample: int) -> bytes:
    """A standalone FLAC file at the source's bit depth (lossless)."""
    subtype = _FLAC_SUBTYPES.get(bits_per_sample)
    if subtype is None:
        raise OndioError(
            f"cannot re-encode {bits_per_sample}-bit FLAC: soundfile writes only 8, 16"
            " and 24 bits. read_flac(..., decode=True) still returns the window's samples."
        )
    buf = io.BytesIO()
    sf.write(buf, samples, sample_rate, format="FLAC", subtype=subtype)
    return buf.getvalue()


@dataclass
class _Fetch:
    """Bytes fetched by range, and the frame headers found in them."""

    buf: bytes
    lo: int  # absolute offset of buf[0]
    found: dict[int, Frame]  # by absolute offset
    file_size: int

    @property
    def end(self) -> int:
        """The absolute offset just past the fetched bytes."""
        return self.lo + len(self.buf)

    def chain(self, layout: StreamLayout, cover: int) -> list[Frame]:
        """The best chain of the found frames for sample `cover`."""
        candidates = sorted(self.found.values(), key=lambda f: f.offset)
        return find_frame_chain(self.buf, self.lo, layout, cover=cover, candidates=candidates)

    def extend(self, backend: StorageBackend, uri: str, layout: StreamLayout, new_lo: int, new_end: int) -> None:
        """Widen to bytes [new_lo, new_end) and add the frames in the new bytes.

        Only the new bytes are scanned, plus a header's length before the right
        edge, where a header cut short by the old edge now parses.
        """
        if new_lo < self.lo:
            chunk = _read_exact(backend, uri, new_lo, self.lo, self.file_size)
            self.buf, self.lo = chunk + self.buf, new_lo
            self.found.update((f.offset, f) for f in scan_frames(self.buf, self.lo, layout, 0, len(chunk)))
        if new_end > self.end:
            chunk = _read_exact(backend, uri, self.end, new_end, self.file_size)
            rescan = max(0, len(self.buf) - MAX_HEADER_BYTES)
            self.buf += chunk
            self.found.update((f.offset, f) for f in scan_frames(self.buf, self.lo, layout, rescan))
        printer.message(f"ranged fetch widened to bytes {self.lo}-{self.end - 1}")


def _read_exact(backend: StorageBackend, uri: str, start: int, end: int, file_size: int) -> bytes:
    """Bytes [start, end), raising if the backend returns fewer before the end of the file.

    A short read would shift the offsets of frames found to its right, and
    could stop the fetch from growing.
    """
    data = bytes(backend.read_range(uri, start, end - 1))
    if len(data) < min(end, file_size) - start:
        raise _decode_error(uri, "backend returned a short range", ranged=True)
    return data


def _chain_end_sample(chain: list[Frame], layout: StreamLayout, fetch: _Fetch) -> int:
    """The sample just past the last chained frame known to be complete.

    Every chained frame but the last ends where the next begins. The last
    is complete if it is the file's final frame. It is also complete when
    a whole frame's worth of bytes follows its start. Otherwise the chain
    is trusted only up to the start of its last frame.
    """
    last = chain[-1]
    if last.end_sample == layout.total_samples and fetch.end == fetch.file_size:
        return last.end_sample
    if fetch.end - last.offset >= layout.frame_bytes_guess:
        return last.end_sample
    return last.first_sample


def _chain_breaks_in_window(
    chain: list[Frame], end_sample: int, window: tuple[int, int], fetch: _Fetch, layout: StreamLayout
) -> bool:
    """True if the chain stops inside the window where the fetch already holds the next frame.

    The frame after the chain starts at most one frame past its last frame;
    the frame before it starts at most one frame before its first frame. If
    that place is in the fetched bytes and has no valid header, the stream
    is damaged inside the window, and a wider fetch cannot help. The margin
    of two frames allows for a frame larger than frame_bytes_guess.
    """
    first, stop = window
    margin = 2 * layout.frame_bytes_guess
    head, last = chain[0], chain[-1]
    if head.first_sample <= first and end_sample == last.end_sample < stop:
        return fetch.end - last.offset >= margin
    if first < head.first_sample < stop:
        return head.offset - fetch.lo >= margin
    return False


def _locate_chain(
    backend: StorageBackend,
    uri: str,
    layout: StreamLayout,
    file_size: int,
    first: int,
    stop: int,
) -> tuple[_Fetch, list[Frame]]:
    """Fetch bytes by range until a chain of frames covers samples [first, stop).

    The byte estimate (seek-table interpolation, else the average bitrate)
    only chooses what to fetch. The frames in the fetched bytes give the
    real position of the audio. They form a chain of CRC-checked headers
    whose sample numbers continue one another. If the chain does not cover
    a side of the window, that side is widened and fetched. Each byte is
    fetched at most once. The loop ends when the chain covers the window,
    when the fetched bytes show damage in the window, or when a short side
    reaches the edge of the audio.
    """

    def fail(reason: str) -> OndioError:
        return _decode_error(uri, reason, ranged=True)

    audio_offset, block = layout.audio_offset, layout.max_blocksize
    slack = layout.frame_bytes_guess
    lo = max(audio_offset, sample_to_byte(layout, file_size, first - block) - slack)
    end = min(file_size, sample_to_byte(layout, file_size, stop + block) + slack + 1)
    buf = _read_exact(backend, uri, lo, end, file_size)
    fetch = _Fetch(buf, lo, {f.offset: f for f in scan_frames(buf, lo, layout)}, file_size)
    bytes_per_sample = (file_size - audio_offset) / layout.total_samples
    printer.message(f"ranged fetch: bytes {lo}-{end - 1} for samples {first}-{stop}")

    def grow_bytes(missing: int, attempt: int) -> int:
        # The average bitrate underestimates loud passages, so fetch half as
        # much again. Doubling each round bounds the number of rounds.
        return int((missing + block) * bytes_per_sample * 1.5 * 2**attempt) + slack

    attempt = 0
    while True:
        chain = fetch.chain(layout, first)
        end_sample = _chain_end_sample(chain, layout, fetch) if chain else 0
        short_left = not chain or chain[0].first_sample > first
        short_right = not chain or end_sample < stop
        if not (short_left or short_right):
            return fetch, chain
        if (short_left and fetch.lo <= audio_offset) or (short_right and fetch.end >= file_size):
            raise fail(
                f"no run of valid frames covers samples {first}-{stop}, though the fetch"
                " reaches the edge of the audio: the stream is corrupt or truncated there"
            )
        if chain and _chain_breaks_in_window(chain, end_sample, (first, stop), fetch, layout):
            raise fail(
                f"the run of valid frames stops inside samples {first}-{stop}:"
                " the stream is corrupt there"
            )
        new_lo, new_end = fetch.lo, fetch.end
        if short_left:
            missing = chain[0].first_sample - first if chain else stop - first
            new_lo = max(audio_offset, fetch.lo - grow_bytes(missing, attempt))
        if short_right:
            missing = stop - end_sample if chain else stop - first
            new_end = min(file_size, fetch.end + grow_bytes(missing, attempt))
        fetch.extend(backend, uri, layout, new_lo, new_end)
        attempt += 1


def _decode_chain(
    uri: str, layout: StreamLayout, fetch: _Fetch, chain: list[Frame], first: int, stop: int
) -> np.ndarray:
    """Samples [first, stop), decoded from the chained frames that hold them.

    Only those frames are decoded, so damage elsewhere in the fetch cannot
    fail a clean window. They are wrapped in a minimal stream. They keep
    their original numbers, so the stream is read sequentially: a seek would
    fail. The decoder stops after n_samples, so bytes past the window's last
    frame are never decoded.
    """
    i0 = max(i for i, f in enumerate(chain) if f.first_sample <= first)
    i1 = min(i for i, f in enumerate(chain) if f.end_sample >= stop)
    head = chain[i0]
    end_byte = chain[i1 + 1].offset if i1 + 1 < len(chain) else fetch.end
    n_samples = chain[i1].end_sample - head.first_sample
    stream = rebuild_stream(layout, fetch.buf[head.offset - fetch.lo:end_byte - fetch.lo], n_samples)
    try:
        with sf.SoundFile(io.BytesIO(stream)) as f:
            samples = f.read(n_samples, dtype="int32", always_2d=True)
    except sf.SoundFileError as exc:
        raise _decode_error(uri, str(exc), ranged=True) from exc
    if len(samples) != n_samples:
        raise _decode_error(uri, f"decoded {len(samples)} of {n_samples} samples", ranged=True)
    offset = first - head.first_sample
    return samples[offset:offset + stop - first]


def _read_window(
    backend: StorageBackend,
    uri: str,
    start_sec: float | None,
    end_sec: float | None,
    *,
    use_range: bool | None,
) -> tuple[np.ndarray, StreamLayout]:
    """Shared core: fetch, decode and cut the requested window by sample.

    start_sec=None means 0; end_sec=None means the end of the stream. Returns
    soundfile's int32 samples [round(start_sec * sr), round(end_sec * sr)),
    shape (frames, channels), with the end clamped to the stream, and the
    stream's layout (sample rate, bit depth). The metadata is fetched once.
    """
    # Caller errors are ValueError; stream/IO problems are OndioError.
    start_sec = 0.0 if start_sec is None else start_sec
    if start_sec < 0:
        raise ValueError(f"start_sec must be >= 0, got {start_sec}")
    if end_sec is not None and end_sec <= start_sec:
        raise ValueError(f"end_sec ({end_sec}) must be > start_sec ({start_sec})")

    layout = parse_layout(backend, uri, seektable=True)
    sr = layout.sample_rate
    duration = layout.total_samples / sr if sr else 0.0
    # The spec allows total_samples = 0 ("unknown length"); window validation
    # and positioning both need a real duration.
    if duration <= 0:
        raise OndioError(f"FLAC stream reports zero duration: {uri}")
    if start_sec >= duration:
        raise ValueError(f"start_sec ({start_sec}) is past the end of the stream ({duration:.3f}s)")
    # Clamp, like Python slices past the end.
    if end_sec is None:
        end_sec = duration
    elif end_sec > duration:
        printer.message(f"end_sec {end_sec} exceeds stream duration {duration:.3f}s, clamping")
        end_sec = duration
    printer.message(f"windowed FLAC read: {uri}, window {start_sec:.2f}-{end_sec:.2f}s")

    first = round(start_sec * sr)
    stop = min(round(end_sec * sr), layout.total_samples)
    if first >= stop:
        raise ValueError(
            f"window {start_sec}-{end_sec}s holds no samples: at {sr} Hz it is"
            " shorter than one sample period"
        )

    file_size = backend.size(uri)
    if use_range is None:
        use_range = (
            file_size > _RANGE_MIN_FILE_BYTES
            and (end_sec - start_sec) / duration < _RANGE_MAX_WINDOW_RATIO
        )
    printer.message(
        f"file info: duration={duration:.1f}s, size={file_size / 2**20:.1f}MB,"
        f" window ratio={(end_sec - start_sec) / duration:.2f}, use_range={use_range}"
    )
    if use_range:
        fetch, chain = _locate_chain(backend, uri, layout, file_size, first, stop)
        return _decode_chain(uri, layout, fetch, chain, first, stop), layout

    samples = _decode_span(uri, backend.read(uri), first, stop)
    printer.message(f"full download decoded: samples {first}-{stop}")
    return samples, layout


@overload
def read_flac(
    backend: StorageBackend,
    uri: str,
    start_sec: float | None = None,
    end_sec: float | None = None,
    *,
    decode: Literal[True] = True,
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
    use_range: bool | None = None,
) -> bytes: ...


def read_flac(
    backend: StorageBackend,
    uri: str,
    start_sec: float | None = None,
    end_sec: float | None = None,
    *,
    decode: bool = True,
    use_range: bool | None = None,
) -> tuple[np.ndarray, int] | bytes:
    """Read a FLAC file — whole, or a `[start_sec, end_sec]` window — into memory.

    Windows are cut by sample index: `[round(start_sec * sr),
    round(end_sec * sr))`.

    Args:
        backend: The storage backend to fetch through.
        uri: URI of a FLAC file, addressable by `backend`.
        start_sec: Window start in seconds; `None` means from the beginning.
        end_sec: Window end in seconds; `None` means to the end of the stream
            (values past EOF are clamped, like Python slices).
        decode: If `True` (default), return decoded PCM; if `False`, return the
            bytes of a standalone FLAC file — the original bytes exactly when
            no window is given (a plain read), a fresh lossless encode of
            the slice otherwise.
        use_range: Fetch strategy. `None` (default) picks per the legacy
            heuristic: ranged download for files > 50 MB when the window is
            < 1/3 of the stream, full download otherwise. `True`/`False` forces
            one path. Both are sample-exact.

    Returns:
        With `decode=True`, `(samples, sample_rate)` where `samples` is a
        `float32` array of shape `(frames, channels)` normalized to `[-1, 1]`
        like libsndfile/`soundfile`. With `decode=False`, FLAC bytes.

    Raises:
        ValueError: If the window is empty, negative, starts past EOF, or
            holds no samples.
        OndioError: If any of these is true:

            - `uri` is not a valid FLAC stream.
            - A window was requested and the stream reports zero duration.
            - The audio to decode is corrupt or truncated.
            - A whole-file read meets a stream of unknown length.
            - `decode=False` must re-encode a window of a source that is
              not 8, 16 or 24 bits deep.

    Note:
        The ranged path returns exactly the samples a full download would.
        A byte estimate chooses what to fetch. The frame numbers in the
        fetched bytes give the window's position. If the frames do not
        cover the window, the fetch widens; it fetches each byte at most
        once, so it never fetches more than the file. For a corrupt or
        truncated region the ranged path raises `OndioError` naming
        `use_range=False`. It never starts a full download.
    """
    whole = start_sec is None and end_sec is None
    if whole and not decode:
        return backend.read(uri)
    if whole:
        return _decode_whole(uri, backend.read(uri))
    samples, layout = _read_window(backend, uri, start_sec, end_sec, use_range=use_range)
    if decode:
        return _to_float32(samples), layout.sample_rate
    return _encode_flac(samples, layout.sample_rate, layout.bits_per_sample)


def download_flac(
    backend: StorageBackend,
    uri: str,
    out_path: str | os.PathLike[str],
    start_sec: float | None = None,
    end_sec: float | None = None,
    *,
    use_range: bool | None = None,
) -> None:
    """Write a `.flac` file containing `[start_sec, end_sec]` of `uri` to `out_path`.

    With no window this is a byte-for-byte download of the original file.
    With one, the window is decoded, cut by sample index, and re-encoded —
    losslessly identical PCM, but not byte-identical to the source. Window
    and fetch semantics, and the ranged-path note, are as in
    [`read_flac`][ondio.flac.read_flac].

    (Counterpart of legacy `soundhub_utils` `io.read_flac(src, dest, ...)`.)

    Args:
        backend: The storage backend to fetch through.
        uri: URI of a FLAC file, addressable by `backend`.
        out_path: Local `.flac` destination path; parent directories are created.
        start_sec: Window start in seconds; `None` means from the beginning.
        end_sec: Window end in seconds; `None` means to the end of the stream.
        use_range: As in [`read_flac`][ondio.flac.read_flac].

    Raises:
        ValueError: If the window is empty, negative, starts past EOF, or
            holds no samples.
        OndioError: If any of these is true:

            - `uri` is not a valid FLAC stream.
            - A window was requested and the stream reports zero duration.
            - The window's audio is corrupt or truncated.
            - The source is not 8, 16 or 24 bits deep, so the window cannot
              be re-encoded.
    """
    if start_sec is None and end_sec is None:
        backend.download(uri, out_path)
        return
    samples, layout = _read_window(backend, uri, start_sec, end_sec, use_range=use_range)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(_encode_flac(samples, layout.sample_rate, layout.bits_per_sample))
