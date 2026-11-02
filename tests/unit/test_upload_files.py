"""upload_files: a local folder to a prefix, against local disk and moto S3."""

import os

import pytest

import ondio
from ondio.backends import LocalBackend

EXPECTED = [".hidden", "a.bin", "sub/b.bin", "sub/deeper/c.bin"]


@pytest.fixture
def src_dir(tmp_path):
    root = tmp_path / "src"
    for name in EXPECTED:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    (root / "empty").mkdir()
    return root


def test_uploads_every_file_sorted_by_relative_path(src_dir, aws_bucket):
    uris = ondio.upload_files(f"{aws_bucket}/run", src_dir)
    assert uris == [f"{aws_bucket}/run/{name}" for name in EXPECTED]
    assert ondio.read(f"{aws_bucket}/run/sub/deeper/c.bin") == (src_dir / "sub/deeper/c.bin").read_bytes()


@pytest.mark.parametrize("suffix", ["", "/"])
def test_bucket_root_gives_keys_with_no_leading_slash(src_dir, aws_bucket, suffix):
    ondio.upload_files(aws_bucket + suffix, src_dir)
    assert ondio.locate(f"{aws_bucket}/a.bin").path == "a.bin"
    assert ondio.list_files(f"{aws_bucket}/", recursive=False) == [
        f"{aws_bucket}/.hidden", f"{aws_bucket}/a.bin",
    ]


def test_local_target_uris_join_the_prefix_and_the_relative_path(src_dir, tmp_path):
    out = tmp_path / "out"
    assert ondio.upload_files(str(out), src_dir) == [f"{out}/{name}" for name in EXPECTED]
    assert (out / "sub" / "deeper" / "c.bin").read_bytes() == b"sub/deeper/c.bin"


def test_trailing_slash_gives_the_same_keys(src_dir, tmp_path):
    out = tmp_path / "out"
    assert ondio.upload_files(str(out), src_dir) == ondio.upload_files(f"{out}/", src_dir)


def test_empty_folders_make_no_objects(src_dir, tmp_path):
    ondio.upload_files(str(tmp_path / "out"), src_dir)
    assert not (tmp_path / "out" / "empty").exists()


def test_an_empty_folder_uploads_nothing(tmp_path):
    (tmp_path / "none").mkdir()
    assert ondio.upload_files(str(tmp_path / "out"), tmp_path / "none") == []


def test_linked_folders_are_not_entered_and_linked_files_are_uploaded(src_dir, tmp_path):
    (src_dir / "link.bin").symlink_to(src_dir / "a.bin")
    (src_dir / "linkdir").symlink_to(src_dir / "sub", target_is_directory=True)
    out = tmp_path / "out"
    uris = ondio.upload_files(str(out), src_dir)
    assert f"{out}/link.bin" in uris
    assert (out / "link.bin").read_bytes() == b"a.bin"
    assert not any("linkdir" in uri for uri in uris)


def test_without_overwrite_an_existing_object_raises_and_is_kept(src_dir, tmp_path):
    out = tmp_path / "out"
    ondio.write(str(out / "sub" / "b.bin"), b"keep")
    with pytest.raises(ondio.ObjectExistsError):
        ondio.upload_files(str(out), src_dir, overwrite=False)
    assert (out / "sub" / "b.bin").read_bytes() == b"keep"


def test_missing_folder_raises_before_any_upload(tmp_path):
    with pytest.raises(FileNotFoundError):
        ondio.upload_files(str(tmp_path / "out"), tmp_path / "nope")
    assert not (tmp_path / "out").exists()


def test_a_file_instead_of_a_folder_raises(tmp_path):
    (tmp_path / "f.bin").write_bytes(b"x")
    with pytest.raises(NotADirectoryError):
        ondio.upload_files(str(tmp_path / "out"), tmp_path / "f.bin")


def test_backend_with_options_is_refused(src_dir, tmp_path):
    with pytest.raises(ValueError):
        ondio.upload_files(str(tmp_path / "out"), src_dir, backend=LocalBackend(), region_name="x")


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permissions, not root")
def test_an_unreadable_folder_raises_before_any_upload(src_dir, tmp_path):
    locked = src_dir / "sub"
    locked.chmod(0)
    try:
        with pytest.raises(PermissionError):
            ondio.upload_files(str(tmp_path / "out"), src_dir)
    finally:
        locked.chmod(0o755)
    assert not (tmp_path / "out").exists()


def test_a_file_root_prefix_keeps_its_three_slashes(src_dir):
    class Recorder:
        def upload(self, uri, source_path):
            pass

    uris = ondio.upload_files("file:///", src_dir, backend=Recorder())
    assert uris == [f"file:///{name}" for name in EXPECTED]


def test_a_broken_link_raises_before_any_upload(src_dir, tmp_path):
    (src_dir / "broken").symlink_to(src_dir / "missing")
    with pytest.raises(FileNotFoundError):
        ondio.upload_files(str(tmp_path / "out"), src_dir)
    assert not (tmp_path / "out").exists()
