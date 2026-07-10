"""upload_parquet tests against the local backend (needs the `parquet` extra)."""

import pytest

pa = pytest.importorskip("pyarrow")
parquet_mod = pytest.importorskip("ondio.parquet")
import pyarrow.dataset as pads  # noqa: E402

import ondio  # noqa: E402
from ondio import OndioError  # noqa: E402


@pytest.fixture
def table():
    return pa.table(
        {
            "x": [1, 2, 3, 4],
            "site": ["north", "north", "south", "south"],
        }
    )


def read_back(prefix):
    dataset = pads.dataset(prefix, format="parquet", partitioning="hive")
    result = dataset.to_table().to_pydict()
    return sorted(zip(result["x"], [str(s) for s in result["site"]]))


def test_roundtrip_hive_partitioned(tmp_path, table):
    prefix = str(tmp_path / "ds")
    parquet_mod.upload_parquet(prefix, table, ["site"])
    files = ondio.list_files(prefix)
    assert files and all(f.endswith(".parquet") for f in files)
    assert any("site=north" in f for f in files) and any("site=south" in f for f in files)
    assert read_back(prefix) == [(1, "north"), (2, "north"), (3, "south"), (4, "south")]


def test_existing_data_requires_overwrite(tmp_path, table):
    prefix = str(tmp_path / "ds")
    parquet_mod.upload_parquet(prefix, table, ["site"])
    with pytest.raises(OndioError, match="overwrite=True"):
        parquet_mod.upload_parquet(prefix, table, ["site"])


def test_overwrite_replaces(tmp_path, table, capsys):
    prefix = str(tmp_path / "ds")
    parquet_mod.upload_parquet(prefix, table, ["site"])
    smaller = table.slice(0, 2)  # north only
    parquet_mod.upload_parquet(prefix, smaller, ["site"], overwrite=True)
    assert read_back(prefix) == [(1, "north"), (2, "north")]
    assert not any("site=south" in f for f in ondio.list_files(prefix))
    assert "overwrite: deleted" in capsys.readouterr().out


def test_unpartitioned(tmp_path, table):
    prefix = str(tmp_path / "flat")
    parquet_mod.upload_parquet(prefix, table, [])
    assert read_back(prefix) == [(1, "north"), (2, "north"), (3, "south"), (4, "south")]
