"""Shared fixtures, in the legacy soundhub_utils suite's style.

pytest function-style tests, moto for S3 with a fake-credential safety net so
no test can ever reach a real account, synthesized in-memory/tempdir fixtures,
and no real cloud or network anywhere.
"""

from types import SimpleNamespace

import numpy as np
import pytest

AWS_BUCKET = "testbkt"

FLAC_DURATION_SEC = 30.0

# (id, sample_rate, channels, soundfile subtype, bits_per_sample)
FLAC_PROFILES = [
    ("stereo_44k1_16", 44100, 2, "PCM_16", 16),
    ("mono_48k_24", 48000, 1, "PCM_24", 24),
]


@pytest.fixture(autouse=True)
def _aws_safety(monkeypatch):
    """Fake AWS credentials so no test can touch a real account."""
    for var in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.setenv(var, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


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
    sf = pytest.importorskip("soundfile")
    name, sr, channels, subtype, bits = request.param
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
    path = tmp_path_factory.mktemp("flac") / f"{name}.flac"
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
