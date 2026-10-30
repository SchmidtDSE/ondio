"""Run the Python examples in README.md and the docs pages, so the docs cannot drift
from the code.

The blocks of one page run in order in one namespace, as a reader would run them,
in an empty working directory. S3 is moto, with the bucket `my-bucket` the examples
use; `gs://` is the in-memory fake from test_gcs_backend. Both hold
`audio/rec.flac`, a 70 s FLAC. A block that cannot run offline is preceded by
`<!-- docs-test: skip (reason) -->`.
"""

import io
import re
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("google.cloud.storage")
import boto3  # noqa: E402
import soundfile as sf  # noqa: E402
from moto import mock_aws  # noqa: E402
from test_gcs_backend import FakeClient  # noqa: E402

import ondio  # noqa: E402
import ondio.backends.gcs  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PAGES = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
BLOCK = re.compile(r"(<!-- docs-test: skip[^>]*-->\n)?```python\n(.*?)```", re.S)


def _examples(page):
    """The page's Python blocks, in order, without the ones marked to skip."""
    return [code for skip, code in BLOCK.findall(page.read_text(encoding="utf-8")) if not skip]


def _flac():
    buf = io.BytesIO()
    sf.write(buf, np.zeros((70 * 8000, 1), "float32"), 8000, format="FLAC", subtype="PCM_16")
    return buf.getvalue()


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """moto S3 and a fake GCS, each with my-bucket/audio/rec.flac, and an empty cwd."""
    flac = _flac()
    monkeypatch.chdir(tmp_path)
    gcs_store = {"audio/rec.flac": flac}
    real = ondio.backends.gcs.GcsBackend

    def fake_gcs(**kwargs):
        backend = real.__new__(real)
        backend._client = FakeClient(gcs_store)
        return backend

    monkeypatch.setattr(ondio.backends.gcs, "GcsBackend", fake_gcs)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="my-bucket")
        s3.put_object(Bucket="my-bucket", Key="audio/rec.flac", Body=flac)
        yield
    ondio.set_printer(None)  # the logging page changes it


@pytest.mark.parametrize(
    "page",
    [page for page in PAGES if _examples(page)],
    ids=lambda page: str(page.relative_to(ROOT)),
)
def test_the_examples_run(page, storage):
    namespace = {}
    for number, code in enumerate(_examples(page), start=1):
        try:
            exec(compile(code, f"{page.name} example {number}", "exec"), namespace)
        except Exception as exc:
            pytest.fail(f"{page.relative_to(ROOT)}, example {number}:\n{code}\n{exc!r}")
