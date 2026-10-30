"""Shared fixtures, in the legacy soundhub_utils suite's style.

pytest function-style tests, moto for S3 with a fake-credential safety net so
no test can ever reach a real account, synthesized in-memory/tempdir fixtures,
and no real cloud or network anywhere.
"""

from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

AWS_BUCKET = "testbkt"

FLAC_DURATION_SEC = 30.0

# (id, sample_rate, channels, soundfile subtype, bits_per_sample)
FLAC_PROFILES = [
    ("stereo_44k1_16", 44100, 2, "PCM_16", 16),
    ("mono_48k_24", 48000, 1, "PCM_24", 24),
]


@pytest.fixture(autouse=True)
def _aws_safety(monkeypatch, tmp_path):
    """Fake AWS credentials so no test can touch a real account."""
    for var in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.setenv(var, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    # A developer's endpoint settings would send the tests somewhere else. A
    # profile name would fail, because the config file below does not exist.
    for var in (
        "AWS_ENDPOINT_URL_S3",
        "AWS_ENDPOINT_URL",
        "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))


@pytest.fixture
def aws_bucket():
    """An empty moto-backed S3 bucket; yields its s3:// prefix."""
    boto3 = pytest.importorskip("boto3")
    mock_aws = pytest.importorskip("moto").mock_aws
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=AWS_BUCKET)
        yield f"s3://{AWS_BUCKET}"


@pytest.fixture(scope="session", params=FLAC_PROFILES, ids=[p[0] for p in FLAC_PROFILES])
def fx(request, tmp_path_factory):
    """A synthesized 30 s FLAC file plus its decoded reference samples.

    Two profiles: 16-bit stereo 44.1 kHz and 24-bit mono 48 kHz — the 24-bit
    layout is the one legacy ranged reads crashed on (ffprobe misidentified
    bare 24-bit blobs as video). Seeded noise makes any window's samples
    unique, so contiguity checks can locate returned audio in the source.
    """
    return _synth_flac(request.param, tmp_path_factory.mktemp("flac"))


def _synth_flac(profile, directory):
    name, sr, channels, subtype, bits = profile
    rng = np.random.default_rng(7)
    t = np.arange(int(FLAC_DURATION_SEC * sr)) / sr
    sig = np.stack(
        [
            np.clip(
                0.4 * np.sin(2 * np.pi * 220 * (c + 1) * t) + 0.05 * rng.standard_normal(t.size),
                -0.99,
                0.99,
            )
            for c in range(channels)
        ],
        axis=1,
    )
    path = directory / f"{name}.flac"
    sf.write(path, sig, sr, format="FLAC", subtype=subtype)
    data, _ = sf.read(path, dtype="float32", always_2d=True)
    return SimpleNamespace(
        path=path,
        uri=str(path),
        sr=sr,
        channels=channels,
        bits=bits,
        duration=FLAC_DURATION_SEC,
        data=data,
    )


@pytest.fixture(scope="session")
def fx_damaged(tmp_path_factory):
    """Damaged copies of the 48 kHz mono profile, with STREAMINFO untouched.

    `truncated` keeps the first 60% of the bytes (real audio ends near 18 s
    while the header still claims 30 s, like real recorder files);
    `corrupt` XORs every 7th byte across 3,000 bytes at the file's midpoint.
    `data` is the clean decoded reference.
    """
    directory = tmp_path_factory.mktemp("flac_damaged")
    clean = _synth_flac(FLAC_PROFILES[1], directory)
    raw = clean.path.read_bytes()
    truncated = directory / "truncated.flac"
    truncated.write_bytes(raw[: int(len(raw) * 0.6)])
    corrupt = bytearray(raw)
    mid = len(raw) // 2
    for i in range(mid, mid + 3000, 7):
        corrupt[i] ^= 0x5A
    corrupt_path = directory / "corrupt.flac"
    corrupt_path.write_bytes(bytes(corrupt))
    return SimpleNamespace(
        truncated=str(truncated),
        corrupt=str(corrupt_path),
        sr=clean.sr,
        data=clean.data,
    )


@pytest.fixture(scope="session")
def fx_vbr(tmp_path_factory):
    """30 s, 48 kHz mono, 16-bit: near-silent noise (1e-4) for 0-10 s, loud
    noise (0.5) for 10-30 s. The quiet third compresses to almost nothing, so
    a linear seconds-to-bytes estimate misplaces mid-file windows by seconds.
    soundfile writes no seek table.
    """
    sr = 48000
    rng = np.random.default_rng(11)
    sig = rng.uniform(-1, 1, int(FLAC_DURATION_SEC * sr))
    sig[: 10 * sr] *= 1e-4
    sig[10 * sr:] *= 0.5
    path = tmp_path_factory.mktemp("flac_vbr") / "vbr.flac"
    sf.write(path, sig[:, None], sr, format="FLAC", subtype="PCM_16")
    data, _ = sf.read(path, dtype="float32", always_2d=True)
    return SimpleNamespace(path=path, uri=str(path), sr=sr, channels=1, duration=FLAC_DURATION_SEC, data=data)
