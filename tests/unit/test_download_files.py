"""download_files: plural download with input-order returns and loud collisions.

Composed on the public `download`, so per-URI dispatch (mixed schemes in one
call) and parent-directory creation come from the singular op; what's tested
here is the plural contract — input-order results, upfront basename/collision
validation before any bytes move, fail-fast on the first error, and the
empty-input short-circuit.
"""

from pathlib import Path

import pytest

import ondio
from ondio import ObjectNotFoundError


@pytest.fixture
def src_dir(tmp_path):
    """Three local source files whose contents name themselves."""
    d = tmp_path / "src"
    d.mkdir()
    for name in ("a.jsonl", "b.jsonl", "c.jsonl"):
        (d / name).write_bytes(name.encode())
    return d


def test_downloads_all_and_preserves_input_order(src_dir, tmp_path):
    dest = tmp_path / "dest"
    uris = [str(src_dir / n) for n in ("c.jsonl", "a.jsonl", "b.jsonl")]
    paths = ondio.download_files(uris, dest)
    assert paths == [str(dest / n) for n in ("c.jsonl", "a.jsonl", "b.jsonl")]
    assert [Path(p).read_bytes() for p in paths] == [b"c.jsonl", b"a.jsonl", b"b.jsonl"]


def test_empty_input_returns_empty_and_creates_nothing(tmp_path):
    dest = tmp_path / "dest"
    assert ondio.download_files([], dest) == []
    assert not dest.exists()


def test_duplicate_basenames_raise_before_any_download(src_dir, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "a.jsonl").write_bytes(b"impostor")
    dest = tmp_path / "dest"
    with pytest.raises(ValueError, match="a.jsonl"):
        ondio.download_files([str(src_dir / "a.jsonl"), str(other / "a.jsonl")], dest)
    assert not dest.exists()  # validation ran before any download


def test_prefix_like_uri_raises(tmp_path):
    with pytest.raises(ValueError, match="filename"):
        ondio.download_files(["s3://bucket/prefix/"], tmp_path / "dest")


def test_missing_object_fails_fast(src_dir, tmp_path):
    uris = [str(src_dir / "a.jsonl"), str(src_dir / "missing.jsonl")]
    with pytest.raises(ObjectNotFoundError):
        ondio.download_files(uris, tmp_path / "dest")


def test_mixed_schemes_dispatch_per_uri(src_dir, tmp_path):
    requests_mock = pytest.importorskip("requests_mock")
    dest = tmp_path / "dest"
    with requests_mock.Mocker() as m:
        m.get("http://files.example.com/remote.jsonl", content=b"remote")
        paths = ondio.download_files(
            [str(src_dir / "a.jsonl"), "http://files.example.com/remote.jsonl"], dest
        )
    assert Path(paths[0]).read_bytes() == b"a.jsonl"
    assert Path(paths[1]).read_bytes() == b"remote"


def test_basename_ignores_query_string(tmp_path):
    requests_mock = pytest.importorskip("requests_mock")
    dest = tmp_path / "dest"
    with requests_mock.Mocker() as m:
        m.get("http://files.example.com/f.jsonl?token=abc", content=b"q")
        paths = ondio.download_files(["http://files.example.com/f.jsonl?token=abc"], dest)
    assert paths == [str(dest / "f.jsonl")]
    assert Path(paths[0]).read_bytes() == b"q"


def test_s3_batch_with_backend_kwargs(aws_bucket, tmp_path):
    for i in range(3):
        ondio.write(f"{aws_bucket}/results/results_{i}.jsonl", f"row{i}".encode())
    uris = ondio.list_files(f"{aws_bucket}/results/")
    paths = ondio.download_files(uris, tmp_path / "dl", region_name="us-east-1")
    assert [Path(p).name for p in paths] == [
        "results_0.jsonl",
        "results_1.jsonl",
        "results_2.jsonl",
    ]
    assert Path(paths[1]).read_bytes() == b"row1"
