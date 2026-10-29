"""Run routes: upload (streamed, bounded), run list, transforms and preview.

``TestClient`` (in-process ASGI) with an in-memory store and a ``LocalBlobStore`` under
``tmp_path``; the bounded-read tests drive the ASGI app directly with a counting body stream.
Nothing binds a port.
"""

from __future__ import annotations

import asyncio
import io
import json
import unicodedata
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from api_fakes import FakeStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sluice.agent import events as ev
from sluice.agent.events import EventLog, EventModel
from sluice.api.app import create_app
from sluice.api.blobs import LocalBlobStore
from sluice.api.routes_runs import (
    MAX_FIELD_BYTES,
    PREVIEW_SAVE_GRACE_S,
    TOO_LARGE_DETAIL,
    sanitise_file_name,
    upload_extension,
)
from sluice.api.schemas import RunSummary
from sluice.api.settings import MAX_UPLOAD_BYTES, ApiSettings
from sluice.api.store import RunRow, iso_utc
from sluice.contracts.model import load_contract
from sluice.runs.execute import file_sha256
from sluice.runs.preview import page_preview

CSV = b"o_orderkey,o_custkey\n1,10\n2,20\n"
SUMMARY_KEYS = {"id", "fileName", "table", "rows", "status", "createdAt", "costUsd"}


class RouteStore(FakeStore):
    """``FakeStore`` plus the read/insert methods the routes call."""

    def __init__(self) -> None:
        super().__init__()
        self.list_limits: list[int] = []

    def create_run(
        self, *, run_id: str, file_name: str, table: str, blob_key: str, file_sha256: str
    ) -> RunSummary:
        with self.lock:
            from datetime import UTC, datetime

            self.runs[run_id] = RunRow(
                id=run_id,
                file_name=file_name,
                table=table,  # type: ignore[arg-type]
                rows=None,
                status="queued",
                created_at=iso_utc(datetime.now(UTC)),
                cost_usd=0.0,
                blob_key=blob_key,
                file_sha256=file_sha256,
                started_at=None,
                finished_at=None,
            )
            self.events[run_id] = []
            return self.runs[run_id].summary()

    def list_runs(self, limit: int = 100) -> list[RunSummary]:
        self.list_limits.append(limit)
        with self.lock:
            rows = sorted(self.runs.values(), key=lambda r: r.created_at, reverse=True)
            return [r.summary() for r in rows[:limit]]

    def get_artefacts(self, run_id: str) -> Any:
        with self.lock:
            return self.artefacts.get(run_id)


class FakeWorker:
    def __init__(self) -> None:
        self.notified = 0

    def notify(self) -> None:
        self.notified += 1

    async def stop(self) -> None:  # the lifespan stops whatever worker is on app.state
        return None


@pytest.fixture
def store() -> RouteStore:
    return RouteStore()


@pytest.fixture
def blobs(tmp_path: Path) -> LocalBlobStore:
    return LocalBlobStore(tmp_path / "blobs")


def _app(store: RouteStore, blobs: LocalBlobStore, **settings: Any) -> FastAPI:
    return create_app(settings=ApiSettings(**settings), store=store, blobs=blobs, worker=False)  # type: ignore[arg-type]


@pytest.fixture
def worker() -> FakeWorker:
    return FakeWorker()


@pytest.fixture
def client(store: RouteStore, blobs: LocalBlobStore, worker: FakeWorker) -> Iterator[TestClient]:
    app = _app(store, blobs)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        app.state.worker = worker  # after the lifespan has set it to None
        yield test_client


def _files(blobs: LocalBlobStore) -> list[str]:
    if not blobs.root.exists():
        return []
    return sorted(str(p.relative_to(blobs.root)) for p in blobs.root.rglob("*") if p.is_file())


def _post(
    client: TestClient, name: str = "orders.csv", data: bytes = CSV, table: str | None = "orders"
) -> Any:
    form = {} if table is None else {"table": table}
    return client.post("/runs", files={"file": (name, data, "application/octet-stream")}, data=form)


# --------------------------------------------------------------------------------------------
# POST /runs: happy path
# --------------------------------------------------------------------------------------------


def test_post_runs_csv_returns_201_queued_summary(client: TestClient) -> None:
    res = _post(client)
    assert res.status_code == 201
    body = res.json()
    assert set(body) == SUMMARY_KEYS
    assert body["fileName"] == "orders.csv"
    assert body["table"] == "orders"
    assert body["status"] == "queued"
    assert body["rows"] is None
    assert body["costUsd"] == 0.0


def test_post_runs_stores_blob_under_generated_key_with_sha(
    client: TestClient, store: RouteStore, blobs: LocalBlobStore
) -> None:
    run_id = _post(client).json()["id"]
    row = store.runs[run_id]
    assert row.blob_key == f"uploads/{run_id}/input.csv"
    stored = blobs.root / row.blob_key
    assert stored.read_bytes() == CSV
    assert row.file_sha256 == file_sha256(stored)


def test_post_runs_notifies_worker(client: TestClient, worker: FakeWorker) -> None:
    _post(client)
    assert worker.notified == 1


@pytest.mark.parametrize(
    ("name", "data", "ext"),
    [
        ("o.json", b'[{"o_orderkey": "1"}]', ".json"),
        ("O.CSV", CSV, ".csv"),
    ],
)
def test_post_runs_accepts_json_and_uppercase_extensions(
    client: TestClient, store: RouteStore, name: str, data: bytes, ext: str
) -> None:
    res = _post(client, name=name, data=data)
    assert res.status_code == 201
    assert store.runs[res.json()["id"]].blob_key.endswith(f"/input{ext}")


def test_post_runs_accepts_parquet(client: TestClient) -> None:
    buffer = io.BytesIO()
    pl.DataFrame({"o_orderkey": [1, 2]}).write_parquet(buffer)
    res = _post(client, name="o.parquet", data=buffer.getvalue(), table="lineitem")
    assert res.status_code == 201
    assert res.json()["table"] == "lineitem"


def test_post_runs_table_before_file_is_accepted(client: TestClient) -> None:
    boundary = "b0undary"
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="table"\r\n\r\norders\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.csv"\r\n'
            "Content-Type: text/csv\r\n\r\n"
        ).encode()
        + CSV
        + f"\r\n--{boundary}--\r\n".encode()
    )
    res = client.post(
        "/runs", content=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )
    assert res.status_code == 201


# --------------------------------------------------------------------------------------------
# POST /runs: rejections
# --------------------------------------------------------------------------------------------


def _assert_nothing_kept(store: RouteStore, blobs: LocalBlobStore) -> None:
    assert store.runs == {}
    assert _files(blobs) == []


def test_post_runs_unknown_table_is_422_unknown_table(
    client: TestClient, store: RouteStore, blobs: LocalBlobStore
) -> None:
    res = _post(client, table="customers")
    assert res.status_code == 422
    assert res.json()["code"] == "unknown_table"
    assert set(res.json()) == {"detail", "code"}
    _assert_nothing_kept(store, blobs)


def test_post_runs_missing_table_is_422_invalid_request(
    client: TestClient, store: RouteStore, blobs: LocalBlobStore
) -> None:
    res = _post(client, table=None)
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_request"
    _assert_nothing_kept(store, blobs)


def test_post_runs_missing_file_is_422_invalid_request(
    client: TestClient, store: RouteStore, blobs: LocalBlobStore
) -> None:
    res = client.post("/runs", data={"table": "orders"}, files={"other": ("x.txt", b"x")})
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_request"
    _assert_nothing_kept(store, blobs)


def test_post_runs_not_multipart_is_422_invalid_request(client: TestClient) -> None:
    res = client.post("/runs", json={"table": "orders"})
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_request"


def test_post_runs_second_file_part_is_422(
    client: TestClient, store: RouteStore, blobs: LocalBlobStore
) -> None:
    res = client.post(
        "/runs",
        data={"table": "orders"},
        files=[("file", ("a.csv", CSV)), ("file", ("b.csv", CSV))],
    )
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_request"
    _assert_nothing_kept(store, blobs)


def test_post_runs_oversized_table_field_is_422(client: TestClient, store: RouteStore) -> None:
    res = _post(client, table="o" * (MAX_FIELD_BYTES + 1))
    assert res.status_code == 422
    assert store.runs == {}


@pytest.mark.parametrize("name", ["orders.txt", "orders", "orders.csv.exe", "orders.xlsx"])
def test_post_runs_unsupported_extension_is_415(
    client: TestClient, store: RouteStore, blobs: LocalBlobStore, name: str
) -> None:
    res = _post(client, name=name)
    assert res.status_code == 415
    assert res.json()["code"] == "unsupported_format"
    _assert_nothing_kept(store, blobs)


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("bad.parquet", b"definitely not parquet"),
        ("bad.json", b'{"not": "an array"}'),
        ("bad.json", b"[{"),
    ],
)
def test_post_runs_unparseable_is_422_and_blob_deleted(
    client: TestClient, store: RouteStore, blobs: LocalBlobStore, name: str, data: bytes
) -> None:
    res = _post(client, name=name, data=data)
    assert res.status_code == 422
    assert res.json()["code"] == "unparseable"
    _assert_nothing_kept(store, blobs)


def test_post_runs_too_large_is_413_with_contract_detail(
    store: RouteStore, blobs: LocalBlobStore
) -> None:
    app = _app(store, blobs, max_upload_bytes=1000)
    with TestClient(app, raise_server_exceptions=False) as client:
        res = _post(client, data=b"a,b\n" + b"1,2\n" * 300)
    assert res.status_code == 413
    assert res.json() == {"detail": TOO_LARGE_DETAIL, "code": "too_large"}
    assert TOO_LARGE_DETAIL == "File too large: the limit is 100 MB."
    _assert_nothing_kept(store, blobs)


def test_post_runs_path_traversal_name_is_sanitised(
    client: TestClient, store: RouteStore, blobs: LocalBlobStore
) -> None:
    res = _post(client, name="../../etc/passwd.csv")
    assert res.status_code == 201
    run_id = res.json()["id"]
    assert res.json()["fileName"] == "passwd.csv"
    assert store.runs[run_id].blob_key.startswith(f"uploads/{run_id}/")
    assert _files(blobs) == [f"uploads/{run_id}/input.csv"]


def test_post_runs_never_echoes_blob_key_or_sha(client: TestClient) -> None:
    text = _post(client).text
    assert "uploads/" not in text
    assert "blobKey" not in text and "sha" not in text.lower()


# --------------------------------------------------------------------------------------------
# POST /runs: bounded reads (raw ASGI with a counting body stream)
# --------------------------------------------------------------------------------------------

CHUNK = 1024 * 1024
BOUNDARY = "sluiceboundary"


class CountingBody:
    """Multipart body of ``data_bytes`` file bytes, served one ``CHUNK`` per ``receive``."""

    def __init__(self, data_bytes: int, *, table_first: bool = True) -> None:
        head = ""
        if table_first:
            head += f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="table"\r\n\r\n'
            head += "orders\r\n"
        head += (
            f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="big.csv"\r\n'
            "Content-Type: text/csv\r\n\r\n"
        )
        self.head = head.encode()
        self.tail = f"\r\n--{BOUNDARY}--\r\n".encode()
        self.data_bytes = data_bytes
        self.pulled = 0
        self.done = False
        self._chunks = self._gen()

    def _gen(self) -> Iterator[bytes]:
        yield self.head
        block = b"x" * CHUNK
        remaining = self.data_bytes
        while remaining > 0:
            piece = block if remaining >= CHUNK else block[:remaining]
            remaining -= len(piece)
            yield piece
        yield self.tail

    async def receive(self) -> dict[str, Any]:
        if self.done:
            await asyncio.sleep(3600)
        chunk = next(self._chunks, None)
        if chunk is None:
            self.done = True
            return {"type": "http.request", "body": b"", "more_body": False}
        self.pulled += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": True}


async def _drive(
    app: FastAPI, body: CountingBody, extra_headers: list[tuple[bytes, bytes]] | None = None
) -> tuple[int, dict[str, Any]]:
    headers = [(b"content-type", f"multipart/form-data; boundary={BOUNDARY}".encode())]
    headers += extra_headers or []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/runs",
        "raw_path": b"/runs",
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
        "state": {},
    }
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, body.receive, send)  # type: ignore[arg-type]
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(payload)


def test_post_runs_chunked_101mb_stops_after_limit_plus_one_chunk(
    store: RouteStore, blobs: LocalBlobStore
) -> None:
    body = CountingBody(101 * 1024 * 1024)
    status, payload = asyncio.run(_drive(_app(store, blobs), body))
    assert status == 413
    assert payload == {"detail": TOO_LARGE_DETAIL, "code": "too_large"}
    assert body.pulled <= len(body.head) + MAX_UPLOAD_BYTES + CHUNK
    assert not body.done
    _assert_nothing_kept(store, blobs)


def test_post_runs_chunked_upload_just_under_limit_succeeds(
    store: RouteStore, blobs: LocalBlobStore
) -> None:
    body = CountingBody(4096)
    status, payload = asyncio.run(_drive(_app(store, blobs, max_upload_bytes=4096), body))
    # 4096 bytes of "x" is a valid one-column CSV header with no rows.
    assert status == 201, payload
    assert body.done


def test_post_runs_content_length_over_limit_is_413_before_reading(
    store: RouteStore, blobs: LocalBlobStore
) -> None:
    body = CountingBody(10 * CHUNK)
    app = _app(store, blobs, max_upload_bytes=CHUNK)
    length = str(len(body.head) + 10 * CHUNK + len(body.tail)).encode()
    status, payload = asyncio.run(_drive(app, body, [(b"content-length", length)]))
    assert status == 413
    assert payload["code"] == "too_large"
    assert body.pulled == 0
    _assert_nothing_kept(store, blobs)


def test_post_runs_oversized_non_file_parts_are_413(
    store: RouteStore, blobs: LocalBlobStore
) -> None:
    """Bytes outside the file part count too: a huge ignored field cannot bypass the cap."""
    app = _app(store, blobs, max_upload_bytes=CHUNK)
    pad = b"p" * (3 * CHUNK)
    body = (
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="junk"\r\n\r\n'.encode()
        + pad
        + f"\r\n--{BOUNDARY}--\r\n".encode()
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        res = client.post(
            "/runs",
            content=body,
            headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
        )
    assert res.status_code == 413
    _assert_nothing_kept(store, blobs)


# --------------------------------------------------------------------------------------------
# File names
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd.csv", "passwd.csv"),
        ("C:\\Users\\me\\orders.csv", "orders.csv"),
        ("ord\x00er\x1bs\x7f.csv", "orders.csv"),
        ("  spaced.csv  ", "spaced.csv"),
        ("", "upload.csv"),
        ("../", "upload.csv"),
        ("\x00\x01", "upload.csv"),
    ],
)
def test_sanitise_file_name_cases(raw: str, expected: str) -> None:
    assert sanitise_file_name(raw, ".csv") == expected


def test_sanitise_file_name_nfc_normalises() -> None:
    decomposed = "cafe\u0301.csv"
    out = sanitise_file_name(decomposed, ".csv")
    assert out == unicodedata.normalize("NFC", decomposed)
    assert out == "caf\u00e9.csv"


def test_sanitise_file_name_caps_length_keeping_extension() -> None:
    out = sanitise_file_name("a" * 400 + ".parquet", ".parquet")
    assert len(out) == 255
    assert out.endswith(".parquet")


@pytest.mark.parametrize(
    ("name", "ext"),
    [
        ("a.csv", ".csv"),
        ("A.Parquet", ".parquet"),
        ("x.JSON", ".json"),
        ("a.txt", None),
        ("csv", None),
        ("a.csv ", None),
        ("a.\uff43sv", None),
    ],
)
def test_upload_extension(name: str, ext: str | None) -> None:
    assert upload_extension(name) == ext


# --------------------------------------------------------------------------------------------
# GET /runs
# --------------------------------------------------------------------------------------------


def test_get_runs_lists_newest_first_with_limit_100(client: TestClient, store: RouteStore) -> None:
    first = _post(client).json()["id"]
    second = _post(client, name="b.csv").json()["id"]
    older = {"created_at": "2026-01-01T00:00:00.000Z"}
    store.runs[first] = store.runs[first].model_copy(update=older)
    res = client.get("/runs")
    assert res.status_code == 200
    body = res.json()
    assert [r["id"] for r in body] == [second, first]
    assert all(set(r) == SUMMARY_KEYS for r in body)
    assert store.list_limits == [100]


def test_get_runs_empty_is_empty_list(client: TestClient) -> None:
    assert client.get("/runs").json() == []


# --------------------------------------------------------------------------------------------
# Events helper
# --------------------------------------------------------------------------------------------


def _record(store: RouteStore, run_id: str, events: list[EventModel]) -> None:
    log = EventLog()
    for event in events:
        store.append_event(run_id, log.emit(event))


def _node(node_id: str) -> ev.PlanNode:
    return ev.PlanNode(id=node_id, title=node_id, columns=["c"], tier="small", depends_on=[])


def _transform(node_id: str, attempt: int, passed: bool) -> dict[str, Any]:
    return {
        "transform_id": f"t-{node_id}-{attempt}",
        "node_id": node_id,
        "attempt": attempt,
        "code": f"df  # {node_id} {attempt}",
        "model": "groq/x",
        "passed": passed,
    }


# --------------------------------------------------------------------------------------------
# GET /runs/{id}/transforms
# --------------------------------------------------------------------------------------------


def test_get_transforms_unknown_run_is_404(client: TestClient) -> None:
    res = client.get("/runs/nope/transforms")
    assert res.status_code == 404
    assert res.json()["code"] == "not_found"


def test_get_transforms_invalid_id_is_404(client: TestClient) -> None:
    assert client.get("/runs/bad%20id/transforms").status_code == 404


def test_get_transforms_without_artefacts_is_empty(client: TestClient, store: RouteStore) -> None:
    store.add("r1", status="running")
    res = client.get("/runs/r1/transforms")
    assert res.status_code == 200
    assert res.json() == []


def test_get_transforms_in_plan_order_then_attempt(client: TestClient, store: RouteStore) -> None:
    store.add("r1", status="succeeded")
    _record(
        store,
        "r1",
        [ev.PlanCreated(nodes=[_node("n-b"), _node("n-a")]), ev.RulesApplied(actions=[])],
    )
    store.save_artefacts(
        "r1",
        preview_key=None,
        transforms=[
            _transform("n-a", 1, True),
            _transform("n-b", 2, True),
            _transform("n-b", 1, False),
        ],
    )
    res = client.get("/runs/r1/transforms")
    assert res.status_code == 200
    body = res.json()
    assert [(t["nodeId"], t["attempt"]) for t in body] == [("n-b", 1), ("n-b", 2), ("n-a", 1)]
    assert body[0] == {
        "id": "t-n-b-1",
        "nodeId": "n-b",
        "attempt": 1,
        "language": "polars",
        "code": "df  # n-b 1",
        "passed": False,
    }


def test_get_transforms_node_missing_from_plan_sorts_last(
    client: TestClient, store: RouteStore
) -> None:
    store.add("r1", status="succeeded")
    _record(store, "r1", [ev.PlanCreated(nodes=[_node("n-a")])])
    store.save_artefacts(
        "r1", preview_key=None, transforms=[_transform("n-z", 1, True), _transform("n-a", 1, True)]
    )
    body = client.get("/runs/r1/transforms").json()
    assert [t["nodeId"] for t in body] == ["n-a", "n-z"]


# --------------------------------------------------------------------------------------------
# GET /runs/{id}/preview
# --------------------------------------------------------------------------------------------


def _preview_frame(n: int) -> pl.DataFrame:
    names = [c.name for c in load_contract("orders").columns]
    rows = [
        {
            "pk": str(i),
            "quarantined": i == 1,
            "reason": "rejected" if i == 1 else None,
            "cells": {
                name: {"before": f"b{i}", "after": f"a{i}", "flagged": i == 2} for name in names
            },
        }
        for i in range(n)
    ]
    return pl.DataFrame(rows)


def _loaded_run(store: RouteStore, blobs: LocalBlobStore, n: int = 5) -> pl.DataFrame:
    store.add("r1", status="succeeded")
    _record(
        store,
        "r1",
        [
            ev.PlanCreated(nodes=[]),
            ev.Loaded(rows_loaded=n, rows_quarantined=1, snapshot_id="snap-1"),
        ],
    )
    frame = _preview_frame(n)
    buffer = io.BytesIO()
    frame.write_parquet(buffer)
    blobs.put_bytes("runs/r1/preview.parquet", buffer.getvalue())
    store.save_artefacts("r1", preview_key="runs/r1/preview.parquet", transforms=[])
    return frame


def test_get_preview_unknown_run_is_404(client: TestClient) -> None:
    res = client.get("/runs/nope/preview")
    assert res.status_code == 404


def test_get_preview_before_loaded_is_409_not_loaded(client: TestClient, store: RouteStore) -> None:
    store.add("r1", status="running")
    _record(store, "r1", [ev.PlanCreated(nodes=[])])
    res = client.get("/runs/r1/preview")
    assert res.status_code == 409
    assert res.json()["code"] == "not_loaded"


def test_get_preview_loaded_but_not_stored_yet_is_409(
    client: TestClient, store: RouteStore
) -> None:
    store.add("r1", status="running")
    _record(store, "r1", [ev.Loaded(rows_loaded=1, rows_quarantined=0, snapshot_id="s")])
    res = client.get("/runs/r1/preview")
    assert res.status_code == 409


def _finished_loaded_run(store: RouteStore, *, finished_s_ago: float) -> None:
    store.add("r1", status="succeeded")
    moment = datetime.now(UTC) - timedelta(seconds=finished_s_ago)
    store.runs["r1"] = store.runs["r1"].model_copy(update={"finished_at": iso_utc(moment)})
    _record(store, "r1", [ev.Loaded(rows_loaded=1, rows_quarantined=0, snapshot_id="s")])


def test_get_preview_finished_but_preview_not_saved_yet_is_409_not_ready(
    client: TestClient, store: RouteStore
) -> None:
    # run_finished (and the terminal status) come before the preview is written (B2).
    _finished_loaded_run(store, finished_s_ago=0.5)
    store.append_transform("r1", _transform("n-a", 1, True))  # live transforms, no preview
    res = client.get("/runs/r1/preview")
    assert res.status_code == 409
    assert res.json()["code"] == "not_ready"


def test_get_preview_finished_and_saved_without_preview_is_409_no_preview(
    client: TestClient, store: RouteStore
) -> None:
    _finished_loaded_run(store, finished_s_ago=0.5)
    store.save_artefacts("r1", preview_key=None, transforms=[])
    res = client.get("/runs/r1/preview")
    assert res.status_code == 409
    assert res.json()["code"] == "no_preview"


def test_get_preview_finished_long_ago_and_never_saved_is_409_no_preview(
    client: TestClient, store: RouteStore
) -> None:
    # The process died between finish and the save: the preview will never come.
    _finished_loaded_run(store, finished_s_ago=PREVIEW_SAVE_GRACE_S + 1)
    res = client.get("/runs/r1/preview")
    assert res.status_code == 409
    assert res.json()["code"] == "no_preview"


def test_get_preview_after_loaded_returns_contract_columns(
    client: TestClient, store: RouteStore, blobs: LocalBlobStore
) -> None:
    frame = _loaded_run(store, blobs)
    res = client.get("/runs/r1/preview", params={"offset": 0, "limit": 2})
    assert res.status_code == 200
    body = res.json()
    assert set(body) == {"columns", "rows", "totalRows", "nextOffset"}
    assert body["columns"] == [c.name for c in load_contract("orders").columns]
    assert body["totalRows"] == 5
    assert body["nextOffset"] == 2
    expected = page_preview(frame, load_contract("orders"), offset=0, limit=2)
    assert body == expected.model_dump(mode="json")


@pytest.mark.parametrize(("offset", "limit"), [(0, 5), (3, 2), (4, 1000), (2, 1), (9, 3)])
def test_get_preview_pages_match_page_preview(
    client: TestClient, store: RouteStore, blobs: LocalBlobStore, offset: int, limit: int
) -> None:
    frame = _loaded_run(store, blobs)
    res = client.get("/runs/r1/preview", params={"offset": offset, "limit": limit})
    assert res.status_code == 200
    expected = page_preview(frame, load_contract("orders"), offset=offset, limit=limit)
    assert res.json() == expected.model_dump(mode="json")


def test_get_preview_defaults_and_caps_limit(
    client: TestClient, store: RouteStore, blobs: LocalBlobStore
) -> None:
    _loaded_run(store, blobs, n=3)
    assert client.get("/runs/r1/preview").json()["nextOffset"] is None
    res = client.get("/runs/r1/preview", params={"limit": 5000})
    assert res.status_code == 200
    assert len(res.json()["rows"]) == 3


@pytest.mark.parametrize("query", [{"offset": -1}, {"limit": 0}, {"limit": -5}, {"offset": "x"}])
def test_get_preview_bad_paging_is_422(
    client: TestClient, store: RouteStore, blobs: LocalBlobStore, query: dict[str, Any]
) -> None:
    _loaded_run(store, blobs)
    res = client.get("/runs/r1/preview", params=query)
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_request"
