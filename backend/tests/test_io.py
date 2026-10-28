import json
from pathlib import Path

import polars as pl
import pytest

from sluice.io import read_input


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
