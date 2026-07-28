from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ondio._log import printer
from ondio.dispatcher import delete, list_files, upload
from ondio.types import OndioError


def write_parquet(
    uri: str,
    table,
    partition_cols: list[str],
    *,
    compression: str = "snappy",
    overwrite: bool = False,
    max_workers: int | None = None,
    **kwargs: Any,
) -> None:
    """Write a `pyarrow.Table` as a hive-partitioned parquet dataset under `uri`.

    The dataset is written to a local temp directory via pyarrow, then uploaded
    file-by-file through the backend — pyarrow's native S3/GCS filesystems are
    deliberately not used, so credentials follow the backend path. Requires
    the `parquet` extra.

    Args:
        uri: Destination prefix for the dataset.
        table: The `pyarrow.Table` to write.
        partition_cols: Columns to hive-partition by (empty for none).
        compression: Parquet compression codec.
        overwrite: If False, refuse to write when data already exists under
            the prefix (no silent merge). If True, delete it first via
            `list_files` + `delete`.
        max_workers: Thread-pool size for the per-file uploads.
        **kwargs: Forwarded to the backend constructor.

    Raises:
        OndioError: If data exists under the prefix and `overwrite=False`.
        ImportError: If the `parquet` extra is missing.
    """
    try:
        import pyarrow.dataset as pads
    except ImportError as exc:
        raise ImportError(
            "write_parquet requires the 'parquet' extra: pip install ondio[parquet]"
        ) from exc

    # The backends give prefixes folder semantics, so listing "results/run-1"
    # can no longer match keys under "results/run-10/". The trailing slash is
    # kept as a permanent second layer (awscli does the same for recursive
    # rm/cp): it also excludes an object stored at the exact key
    # "results/run-1" from the delete set, which is not part of the dataset.
    base = uri.rstrip("/")
    existing = list_files(f"{base}/", **kwargs)
    if existing:
        if not overwrite:
            raise OndioError(
                f"data already exists under {uri} ({len(existing)} objects); "
                f"pass overwrite=True to replace it"
            )
        for old in existing:
            delete(old, **kwargs)
        printer.message(f"overwrite: deleted {len(existing)} existing objects under {uri}")

    with tempfile.TemporaryDirectory() as tmp:
        pads.write_dataset(
            table,
            tmp,
            format="parquet",
            partitioning=partition_cols or None,
            partitioning_flavor="hive" if partition_cols else None,
            file_options=pads.ParquetFileFormat().make_write_options(compression=compression),
        )
        files = sorted(p for p in Path(tmp).rglob("*") if p.is_file())
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    upload, f"{base}/{path.relative_to(tmp).as_posix()}", path, **kwargs
                )
                for path in files
            ]
            try:
                for future in as_completed(futures):
                    future.result()
            except BaseException:
                executor.shutdown(wait=True, cancel_futures=True)
                raise
        printer.message(f"uploaded {len(files)} parquet files to {base}")


