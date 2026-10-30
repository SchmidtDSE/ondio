# FLAC

FLAC-specific IO lives behind the same read/download split. Decoding uses
soundfile, whose wheels bundle libsndfile, so nothing needs to be on PATH.
A time window `[start_sec, end_sec)` is cut by sample index:
`[round(start_sec * sr), round(end_sec * sr))`.

## Header

`extract_flac_header` parses the STREAMINFO block, fetching only header bytes,
never the audio.

```python
import ondio

header = ondio.extract_flac_header("s3://my-bucket/audio/rec.flac")
header.sample_rate, header.channels, header.duration
```

## Decoding a window

`read_flac` with a window decodes it to PCM, like libsndfile: a float32 array of
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

`ondio` estimates the byte range from the file's seek table. If the file has
no seek table, it uses the average bitrate. The estimate only chooses what to
fetch. In the fetched bytes, `ondio` finds a run of real FLAC frames:
CRC-checked headers whose sample numbers continue one another. The frames give
the true first sample, and the window is cut by sample index. If the run does
not cover the window, the fetch is widened. Widening fetches only new bytes,
so a ranged read never fetches more than the file, in a few requests. When the
estimate is far off (no seek table and very uneven bitrate), a ranged read can
fetch a large part of the file.

Damaged input raises an error on both paths. A window that reaches into a corrupt
or truncated region raises `OndioError`. A file whose header overstates its
length (truncated recorder files exist) raises for windows past its real end.
Windows clear of the damage read exactly. If the damage is in the bytes already
fetched, the ranged read raises without widening the fetch. A ranged failure names
`use_range=False`: a full download is never started on your behalf.
`padding_ratio` is deprecated and ignored.

Over HTTP, a windowed `read_flac` or `download_flac`, and `extract_flac_header`,
need a server that honors range requests and sends a `Content-Length`, whatever
`use_range` is: the header is always read with a range request, and the file size
is always checked. Without them these calls raise `UnsupportedOperationError`. To
read from such a server, read the whole file with `read_flac(url)` and cut the
window yourself, or `download` it and read the local copy.
