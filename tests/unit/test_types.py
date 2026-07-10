import dataclasses

import pytest

from ondio import (
    AuthError,
    FlacHeader,
    ObjectNotFoundError,
    OndioError,
    UnknownPlatformError,
    UnsupportedOperationError,
)


def make_header(**overrides):
    fields = dict(
        sample_rate=44100,
        channels=2,
        bits_per_sample=16,
        total_samples=441000,
        md5=b"\x00" * 16,
        audio_data_offset=86,
    )
    fields.update(overrides)
    return FlacHeader(**fields)


def test_duration():
    assert make_header().duration == 10.0


def test_duration_zero_sample_rate():
    assert make_header(sample_rate=0).duration == 0.0


def test_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        make_header().sample_rate = 48000


@pytest.mark.parametrize(
    "exc",
    [UnknownPlatformError, UnsupportedOperationError, ObjectNotFoundError, AuthError],
)
def test_exception_hierarchy(exc):
    assert issubclass(exc, OndioError)
    assert issubclass(OndioError, Exception)
