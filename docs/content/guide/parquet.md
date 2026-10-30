# Parquet

Model results go out as hive-partitioned parquet datasets via
`ondio.parquet.write_parquet` (requires the `parquet` extra for `pyarrow`). The
dataset is written to a local temp directory with `pyarrow`, then uploaded
file by file through the backend. The temporary directory needs enough free
space for the dataset. Credentials follow the same path as other `ondio` calls.

```python
import pyarrow as pa
from ondio.parquet import write_parquet

table = pa.table({
    "site": ["A", "A", "B"],
    "date": ["2026-04-02", "2026-04-02", "2026-04-03"],
    "species": ["GRSP", "WEME", "GRSP"],
    "confidence": [0.91, 0.87, 0.66],
})

# writes s3://my-bucket/results/run-1/site=A/date=2026-04-02/part-0.parquet, ...
write_parquet("s3://my-bucket/results/run-1", table, partition_cols=["site", "date"])

# partition_cols=[] writes a flat (unpartitioned) dataset under the prefix
write_parquet("s3://my-bucket/results/run-2/", table, partition_cols=[])
```

Details of use:

- **`overwrite`** — by default (`overwrite=False`) the call refuses with
  `OndioError` if objects already exist under the prefix. This check is not
  atomic: concurrent writers can both pass it and mix their output. Give each
  run a unique prefix or coordinate writers outside `ondio`. `overwrite=True`
  deletes the existing objects first. If writing fails, the dataset can be
  incomplete.
- **`compression`** — parquet codec, default `"snappy"`.
- **`max_workers`** — the per-file uploads run in a thread pool; this caps its
  size (default lets the executor decide).
- Backend constructor kwargs pass through as everywhere else
  (e.g. `profile_name="fieldwork"` for S3).

There is no `download_parquet`: readers should point their query engine
(pyarrow, DuckDB, polars) at the dataset directly, or fetch individual files
with `ondio.download` / `ondio.list_files`.
