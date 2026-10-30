# FLAC

FLAC-specific IO lives behind the same read/download split. Decoding uses
`soundfile`, whose wheels bundle libsndfile, so nothing needs to be on `PATH`.
A time window `[start_sec, end_sec)` is cut by sample index:
`[round(start_sec * sr), round(end_sec * sr))`.

## How a FLAC file is laid out

![FLAC file layout: marker, metadata blocks, audio frames, and the parts of one frame](../flac-format.svg#only-light)
![FLAC file layout: marker, metadata blocks, audio frames, and the parts of one frame](../flac-format-dark.svg#only-dark)

A FLAC file has three parts, in order: the four bytes `fLaC`, a run of metadata
blocks, and the audio frames. The format is specified in
[RFC 9639](https://www.rfc-editor.org/rfc/rfc9639.html).

**Metadata blocks.** Each block starts with a 4-byte header: a flag marking the
last block, the block type, and the length of the body. The length lets a reader
step over a block without reading its body.

- STREAMINFO is always first and always 34 bytes. It holds the sample rate,
  channel count, bits per sample, and total number of samples.
  `extract_flac_header` returns it as a `FlacHeader`. A total of 0 means the
  encoder did not know the length; windowed reads require a known length and
  raise `OndioError` when it is unknown.
- SEEKTABLE is optional. Each seek point pairs a sample number with the byte
  offset of the frame that starts there, relative to the first audio frame.
  `ondio` uses it to estimate where a window's bytes are.
- Other blocks, such as tags, embedded artwork, and padding, are skipped during
  metadata parsing. The initial 4,096-byte read can include some or all of a
  block, but the parser does not fetch the rest of its body.

**Audio frames.** Each frame holds a block of consecutive samples for every
channel. Its header starts with a sync code and gives the block size, sample
rate, channels, and bit depth, then the frame's position in the stream: a frame
number when every frame holds the same number of samples, or the number of its
first sample when they differ. A CRC-8 checks the header. The subframes follow,
one per channel, holding the compressed samples, and a CRC-16 checks the whole
frame.

**Byte offsets are not times.** Frames usually hold the same number of samples,
but how well they compress depends on the sound, so their sizes in bytes vary. A
byte offset computed from the average bitrate can land far from the time it was
meant for; on long recordings this put older ranged reads off by about 30 s (see
[Migrating from `soundhub_utils`](differences.md#ranged-windowed-flac-reads)).
`ondio` reads the true position from the frame headers in the fetched bytes
rather than trusting the estimate; see
[Ranged FLAC reads](../architecture.md#ranged-flac-reads) for how.

## Header

`extract_flac_header` parses the STREAMINFO block, fetching an initial range and
any additional metadata block headers needed,
never the audio.

```python
import ondio

header = ondio.extract_flac_header("s3://my-bucket/audio/rec.flac")
header.sample_rate, header.channels, header.duration
```

## Decoding a window

`read_flac` with a window decodes it to PCM, like libsndfile: a `float32` array of
shape `(frames, channels)` in `[-1, 1]`, plus the sample rate.

```python
samples, rate = ondio.read_flac("s3://my-bucket/audio/rec.flac", 60.0, 63.0)
```

## FLAC bytes in memory

`decode=False` returns FLAC bytes instead. With a window, they are a standalone
`.flac` of that window, re-encoded losslessly. With no window, they are the
original file's bytes exactly, with no decoding.

```python
clip = ondio.read_flac("s3://my-bucket/audio/rec.flac", 60.0, 63.0, decode=False)
raw = ondio.read_flac("s3://my-bucket/audio/rec.flac", decode=False)
```

## Clips on disk

`download_flac` writes a local `.flac` file. With a window, it cuts the clip; with
no window, it is a plain byte-for-byte download.

```python
ondio.download_flac("s3://my-bucket/audio/rec.flac", "clips/rec_60-63.flac", 60.0, 63.0)
ondio.download_flac("s3://my-bucket/audio/rec.flac", "data/rec.flac")
```

## Windowed reads of remote files

Windowed reads of large remote files fetch only a byte range around the window
instead of the whole object. The ranged path is chosen automatically for files
over 50 MB when the window is under a third of the stream duration;
`use_range=True`/`False` forces either path. A ranged read that succeeds returns
exactly the samples a full download would.

Widening a ranged read fetches each audio byte at most once, usually in a few
requests. The initial metadata read can overlap the audio range, so total
transfer can exceed the file size. A file with no seek table and a very uneven
bitrate can make the read fetch a large part of the file. [Ranged FLAC reads](../architecture.md#ranged-flac-reads) explains how the window is found.

Damaged input raises an error on both paths. A window that reaches into a corrupt
or truncated region raises `OndioError`. A file whose header overstates its
length (truncated recorder files exist) raises for windows past its real end.
Windows clear of the damage read exactly. If the damage is in the bytes already
fetched, the ranged read raises without fetching more. A ranged failure names
`use_range=False`: a full download is never started on your behalf.
`padding_ratio` is deprecated and ignored.

Over HTTP, a windowed `read_flac` or `download_flac`, and `extract_flac_header`,
need a server that honors range requests and sends a `Content-Length`, whatever
`use_range` is: the header is always read with a range request, and the file size
is always checked. Without them these calls raise `UnsupportedOperationError`. To
read from such a server, read the whole file with `read_flac(url)` and cut the
window yourself, or `download` it and read the local copy.
