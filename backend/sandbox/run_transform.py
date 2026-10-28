"""Sandbox entrypoint (runs inside ``sluice-sandbox:dev``, never on the host).

Contract: ``/work/in/transform.py`` defines ``transform(values: pl.Series) -> pl.Series``;
``/work/in/values.parquet`` has one String column ``values``. On success this writes
``/work/out/values.parquet`` (one String column ``values``, same length, uncompressed, which the
host requires) and exits 0; on any failure it writes ``/work/out/error.txt`` and exits 1.
"""

import importlib.util
import sys
import traceback
from pathlib import Path

import polars as pl

IN_DIR = Path("/work/in")
OUT_DIR = Path("/work/out")
COLUMN = "values"
MAX_ERROR_CHARS = 2_000


class TransformError(Exception):
    """The transform broke the contract; the message is reported as is."""


def _load_transform() -> object:
    spec = importlib.util.spec_from_file_location("transform", IN_DIR / "transform.py")
    if spec is None or spec.loader is None:
        raise TransformError("cannot load transform.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn = getattr(module, "transform", None)
    if not callable(fn):
        raise TransformError("transform.py does not define a callable `transform`")
    return fn


def run() -> None:
    values = pl.read_parquet(IN_DIR / "values.parquet")[COLUMN]
    fn = _load_transform()
    out = fn(values.clone())  # type: ignore[operator]
    if not isinstance(out, pl.Series):
        raise TransformError(f"transform returned {type(out).__name__}, expected polars.Series")
    if out.dtype == pl.Null:  # an all-null result has no inferred type
        out = out.cast(pl.String)
    if out.dtype != pl.String:
        raise TransformError(f"transform returned dtype {out.dtype}, expected String")
    if out.len() != values.len():
        raise TransformError(f"transform returned {out.len()} values for {values.len()} inputs")
    pl.DataFrame({COLUMN: out}).write_parquet(
        OUT_DIR / "values.parquet", compression="uncompressed"
    )


def main() -> int:
    try:
        run()
    except TransformError as exc:
        message = str(exc)
    except Exception:
        # The last frames and the exception line are the useful part of a long traceback.
        message = traceback.format_exc()[-MAX_ERROR_CHARS:]
    else:
        return 0
    (OUT_DIR / "error.txt").write_text(message, encoding="utf-8")
    return 1


if __name__ == "__main__":
    sys.exit(main())
