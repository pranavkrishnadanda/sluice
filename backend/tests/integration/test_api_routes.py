"""Run routes on the Compose stack (`make test-integration`): upload with ``table=auto`` and the
raw-input route, against a real RunStore (Postgres, own schema) and S3BlobStore (RustFS, own
bucket). No worker runs, so the runs stay queued: the raw rows are served from creation.
"""

import secrets
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

import polars as pl
import psycopg
import pytest
from fastapi.testclient import TestClient
from pyarrow.fs import S3FileSystem

from sluice.api.app import create_app
from sluice.api.blobs import S3BlobStore
from sluice.api.settings import ApiSettings
from sluice.api.store import RunStore
from sluice.settings import Settings, get_settings

pytestmark = pytest.mark.integration

ROWS = 25


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def store(settings: Settings) -> Iterator[RunStore]:
    schema = f"t_rts_{secrets.token_hex(4)}"
    with psycopg.connect(settings.postgres_dsn, connect_timeout=5, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')  # generated identifier, not user input
    options = quote(f"-csearch_path={schema}", safe="")
    run_store = RunStore(f"{settings.postgres_dsn}?options={options}")
    run_store.ensure_schema()
    try:
        yield run_store
    finally:
        with psycopg.connect(settings.postgres_dsn, connect_timeout=5, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def blobs(settings: Settings) -> Iterator[S3BlobStore]:
    bucket = f"sluice-test-{secrets.token_hex(4)}"
    blob_store = S3BlobStore.from_settings(settings, bucket)
    try:
        yield blob_store
    finally:
        admin = S3FileSystem(
            access_key=settings.rustfs_access_key.get_secret_value(),
            secret_key=settings.rustfs_secret_key.get_secret_value(),
            endpoint_override=settings.s3_endpoint,
            region="us-east-1",
            connect_timeout=5,
            request_timeout=5,
            allow_bucket_deletion=True,
        )
        admin.delete_dir(bucket)


@pytest.fixture
def client(store: RunStore, blobs: S3BlobStore) -> Iterator[TestClient]:
    app = create_app(settings=ApiSettings(), store=store, blobs=blobs, worker=False)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _upload(client: TestClient, path: Path, table: str | None) -> dict:
    data = {} if table is None else {"table": table}
    with path.open("rb") as fh:
        res = client.post("/runs", files={"file": (path.name, fh, "text/csv")}, data=data)
    assert res.status_code == 201, res.text
    return res.json()


def test_auto_upload_resolves_the_table_and_serves_the_raw_rows(
    client: TestClient, store: RunStore, lineitem_df: pl.DataFrame, tmp_path: Path
) -> None:
    csv = tmp_path / "lines.csv"
    lineitem_df.head(ROWS).write_csv(csv)

    summary = _upload(client, csv, "auto")
    page = client.get(f"/runs/{summary['id']}/input", params={"offset": 20, "limit": 10})

    assert summary["table"] == "lineitem"
    row = store.get_run(summary["id"])
    assert row is not None and row.table == "lineitem" and row.status == "queued"
    assert page.status_code == 200
    body = page.json()
    assert body["columns"] == lineitem_df.columns
    assert body["totalRows"] == ROWS and body["nextOffset"] is None
    assert len(body["rows"]) == ROWS - 20
    first = lineitem_df.row(20)
    assert body["rows"][0][0] == str(first[0])


def test_auto_upload_of_an_unrecognisable_file_is_422_and_keeps_nothing(
    client: TestClient, store: RunStore, tmp_path: Path
) -> None:
    csv = tmp_path / "other.csv"
    csv.write_text("a,b\n1,2\n", encoding="utf-8")

    with csv.open("rb") as fh:
        res = client.post("/runs", files={"file": (csv.name, fh, "text/csv")})

    assert res.status_code == 422
    assert res.json()["code"] == "table_unknown"
    assert store.list_runs() == []


def test_get_input_of_an_unknown_run_is_404(client: TestClient) -> None:
    res = client.get("/runs/nope/input")
    assert res.status_code == 404
