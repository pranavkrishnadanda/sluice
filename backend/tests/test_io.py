import json
from pathlib import Path

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sluice import io as sluice_io
from sluice.io import (
    MAX_PARQUET_DECODED_BYTES,
    MAX_PARQUET_ROWS,
    DecodedTooLarge,
    check_decoded_size,
    read_input,
)


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def test_csv_all_strings(tmp_path: Path) -> None:
    df = read_input(_write(tmp_path / "x.csv", b"a,b\n1,2\n"))
    assert df.dtypes == [pl.String, pl.String]
    assert df.row(0) == ("1", "2")


def test_header_only_csv(tmp_path: Path) -> None:
    df = read_input(_write(tmp_path / "x.csv", b"a,b\n"))
    assert df.shape == (0, 2)
    assert df.columns == ["a", "b"]


def test_duplicate_header(tmp_path: Path) -> None:
    df = read_input(_write(tmp_path / "x.csv", b"a,a\n1,2\n"))
    assert df.width == 2
    assert df.columns[0] == "a"
    assert df.columns[1].startswith("a_duplicated")


def test_latin1_header(tmp_path: Path) -> None:
    df = read_input(_write(tmp_path / "x.csv", b"caf\xe9\n1\n"))
    assert df.columns == ["café"]
    assert df.dtypes == [pl.String]


def test_utf8_bom(tmp_path: Path) -> None:
    df = read_input(_write(tmp_path / "x.csv", b"\xef\xbb\xbfa\n1\n"))
    assert df.columns == ["a"]


def test_nested_json(tmp_path: Path) -> None:
    payload = [{"k": 1, "details": {"x": "y"}}]
    df = read_input(_write(tmp_path / "x.json", json.dumps(payload).encode()))
    assert set(df.columns) == {"k", "details.x"}
    assert df["k"].to_list() == ["1"]
    assert df["details.x"].to_list() == ["y"]
    assert all(dtype == pl.String for dtype in df.dtypes)


def test_json_null_stays_null_and_ragged_keys(tmp_path: Path) -> None:
    payload = [{"a": None, "b": 2}, {"a": "z"}]
    df = read_input(_write(tmp_path / "x.json", json.dumps(payload).encode()))
    assert df["a"].to_list() == [None, "z"]
    assert df["b"].to_list() == ["2", None]


def test_parquet_passthrough(tmp_path: Path) -> None:
    path = tmp_path / "x.parquet"
    pl.DataFrame({"n": [1, 2]}, schema={"n": pl.Int64}).write_parquet(path)
    df = read_input(path)
    assert df.schema == {"n": pl.Int64}


def test_unsupported_extension_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        read_input(_write(tmp_path / "x.xlsx", b""))


# ---------------------------------------------------------------- Parquet footer guard (F3p4)


def _bomb(path: Path, rows: int) -> Path:
    """A tiny file that declares ``rows`` rows: one all-null column, RLE-encoded to a few bytes."""
    pq.write_table(pa.table({"o_orderkey": pa.nulls(rows, pa.int64())}), path)
    return path


def _no_full_read(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("the guard must decide from the footer, before any full read")

    monkeypatch.setattr(sluice_io.pl, "read_parquet", refuse)


def test_parquet_guard_limits_are_the_ruling_values() -> None:
    assert (MAX_PARQUET_ROWS, MAX_PARQUET_DECODED_BYTES) == (5_000_000, 1024**3)


def test_parquet_declaring_too_many_rows_is_refused_from_the_footer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _bomb(tmp_path / "bomb.parquet", MAX_PARQUET_ROWS + 1)
    assert path.stat().st_size < 64 * 1024  # highly compressible: a few KB on disk
    _no_full_read(monkeypatch)
    with pytest.raises(DecodedTooLarge):
        read_input(path)


def test_parquet_at_the_row_limit_passes_the_guard(tmp_path: Path) -> None:
    check_decoded_size(_bomb(tmp_path / "ok.parquet", MAX_PARQUET_ROWS))


def test_parquet_over_the_decoded_size_limit_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "wide.parquet"
    pl.DataFrame({"a": list(range(1000)), "b": ["x" * 50] * 1000}).write_parquet(path)
    monkeypatch.setattr(sluice_io, "MAX_PARQUET_DECODED_BYTES", 4096)
    _no_full_read(monkeypatch)
    with pytest.raises(DecodedTooLarge):
        read_input(path)


def test_decoded_size_counts_at_least_eight_bytes_per_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RLE/dictionary pages under-report what a frame of the file costs in memory."""
    path = _bomb(tmp_path / "nulls.parquet", 10_000)
    monkeypatch.setattr(sluice_io, "MAX_PARQUET_DECODED_BYTES", 10_000 * 8 - 1)
    with pytest.raises(DecodedTooLarge):
        check_decoded_size(path)


def test_decoded_size_guard_ignores_csv_and_json(tmp_path: Path) -> None:
    check_decoded_size(_write(tmp_path / "x.csv", b"a\n1\n"))
    check_decoded_size(_write(tmp_path / "x.json", b"[]"))


def test_decoded_too_large_is_a_value_error() -> None:
    assert issubclass(DecodedTooLarge, ValueError)
