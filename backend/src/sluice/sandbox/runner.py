"""Run one generated ``transform(values) -> values`` inside the ``sluice-sandbox:dev`` container.

Generated code never runs in this process. :func:`run_transform` writes the code and the input
column to a fresh per-run directory, starts a locked-down container on it and reads back
``values.parquet`` (or ``error.txt``) once the container has stopped.

Container hardening (see :func:`_container_kwargs`): no network (``network_disabled`` plus
``network_mode="none"``, loopback only), read-only root filesystem, non-root uid 10001, every
capability dropped, ``no-new-privileges``, memory limit with swap equal to it (no swap spill),
pids limit, ``nofile`` limit, size-capped ``noexec`` tmpfs at ``/tmp``, empty environment, the
input mount read-only and only the output mount writable, bounded container logs.

Host-side defences against a hostile container:

* wall-clock timeout, then ``kill``; the out mount is polled and the container killed once it
  holds more than :data:`SANDBOX_OUTPUT_MAX_BYTES` (no disk flooding through the bind mount);
* output files are opened with ``O_NOFOLLOW | O_NONBLOCK`` and must be regular files (a symlink
  to a host file or a FIFO is refused, never followed or blocked on);
* the output parquet must be uncompressed, one String column ``values`` with the input's length,
  and is read dictionary-encoded so its decoded size is checked before it is materialised
  (no decompression or dictionary bomb);
* the container is force-removed and the per-run directory deleted on every path, including
  errors, timeouts and ``KeyboardInterrupt``.

Failures caused by the transform return ``SandboxResult(ok=False, error=...)``. Failures of the
sandbox itself (no Docker daemon, missing image) raise :class:`SandboxError`, so they are not
blamed on the generated code. Other Docker API errors propagate after cleanup.

Docker daemon: :func:`get_docker_client` tries ``docker.from_env()`` (``DOCKER_HOST`` etc.) and,
if that fails, the host of the current Docker CLI context (what ``docker context inspect`` shows),
which is how Colima is found without ``DOCKER_HOST``.

Per-run directories live under ``$SLUICE_SANDBOX_DIR`` (default ``~/.cache/sluice/sandbox``).
On Colima a bind-mounted directory must be inside a path shared into the VM; Colima shares
``$HOME`` but not ``/private/tmp``, which is why the system temp dir is not used.
"""

import contextlib
import io
import logging
import os
import shutil
import stat
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import docker
import docker.errors
import polars as pl
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import requests
from docker.context import ContextAPI
from docker.types import LogConfig, Ulimit

from sluice.agent.caps import (
    MAX_ERROR_CHARS,
    SANDBOX_MEM_MB,
    SANDBOX_OUTPUT_MAX_BYTES,
    SANDBOX_PIDS,
    SANDBOX_TIMEOUT_S,
)

__all__ = [
    "SANDBOX_IMAGE",
    "SandboxError",
    "SandboxResult",
    "get_docker_client",
    "run_transform",
]

log = logging.getLogger(__name__)

SANDBOX_IMAGE: Final = "sluice-sandbox:dev"
"""Image built by ``make sandbox-image`` from ``backend/sandbox/Dockerfile``."""

WORK_DIR_ENV: Final = "SLUICE_SANDBOX_DIR"
"""Environment variable overriding the root of the per-run directories."""

DEFAULT_WORK_ROOT: Final = Path("~/.cache/sluice/sandbox")

CONTAINER_LABEL: Final = {"sluice.sandbox": "1"}
IN_MOUNT: Final = "/work/in"
OUT_MOUNT: Final = "/work/out"
CODE_FILE: Final = "transform.py"
VALUES_FILE: Final = "values.parquet"
ERROR_FILE: Final = "error.txt"
COLUMN: Final = "values"

POLL_INTERVAL_S = 0.25
"""How often the host checks the container and the size of the out mount, in seconds."""

TMPFS_OPTS: Final = "size=64m,noexec,nosuid,nodev"
NOFILE_LIMIT: Final = 256
LOG_MAX_SIZE: Final = "1m"
DOCKER_API_TIMEOUT_S: Final = 30
MAX_OUT_ENTRIES: Final = 1_000
"""More entries than this in the out mount counts as exceeding the output limit."""

# What ``container.wait(timeout=...)`` raises while the container is still running (over a unix
# socket a read timeout surfaces as a ConnectionError).
_STILL_RUNNING = (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError)


class SandboxError(RuntimeError):
    """The sandbox itself is unavailable (no Docker daemon, image not built)."""


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """Outcome of one sandboxed transform.

    ``values`` is set exactly when ``ok``; otherwise ``error`` says why (≤ ``MAX_ERROR_CHARS``).
    """

    ok: bool
    values: pl.Series | None
    error: str | None
    duration_ms: int


class _Rejected(Exception):
    """The output failed validation; the message becomes ``SandboxResult.error``."""


def get_docker_client() -> docker.DockerClient:
    """Connect to the Docker daemon: the environment first, then the current CLI context.

    Raises:
        SandboxError: neither reaches a daemon.
    """
    try:
        client = docker.from_env(timeout=DOCKER_API_TIMEOUT_S)
        client.ping()
        return client
    except docker.errors.DockerException as env_error:
        context = ContextAPI.get_current_context()
        host = getattr(context, "Host", None)
        if not host:
            raise SandboxError(f"Docker daemon not reachable: {env_error}") from env_error
        try:
            client = docker.DockerClient(base_url=host, timeout=DOCKER_API_TIMEOUT_S)
            client.ping()
            return client
        except docker.errors.DockerException as ctx_error:
            raise SandboxError(f"Docker daemon not reachable at {host}: {ctx_error}") from ctx_error


def run_transform(
    code: str,
    values: pl.Series,
    *,
    timeout_s: int = SANDBOX_TIMEOUT_S,
    client: docker.DockerClient | None = None,
) -> SandboxResult:
    """Run ``code``'s ``transform`` on ``values`` in a fresh sandbox container.

    Args:
        code: Python source defining ``transform(values: pl.Series) -> pl.Series``.
        values: The input column; must have dtype String.
        timeout_s: Wall-clock limit for the container, in seconds.
        client: Docker client; :func:`get_docker_client` when omitted.

    Returns:
        ``ok=True`` with the rewritten column (String, same length), or ``ok=False`` and why.

    Raises:
        TypeError: ``values`` is not a String series.
        SandboxError: no Docker daemon, or the sandbox image is missing.
    """
    if values.dtype != pl.String:
        raise TypeError(f"values must be a String series, got {values.dtype}")
    start = time.monotonic()
    docker_client = client if client is not None else get_docker_client()
    try:
        workdir = _make_workdir()
        try:
            _write_inputs(workdir / "in", code, values)
            outcome = _run_container(docker_client, workdir, len(values), timeout_s)
        finally:
            _remove_workdir(workdir)
    finally:
        if client is None:  # opened here, so closed here: no socket leak across many runs
            docker_client.close()
    duration_ms = int((time.monotonic() - start) * 1000)
    if isinstance(outcome, pl.Series):
        return SandboxResult(ok=True, values=outcome, error=None, duration_ms=duration_ms)
    return SandboxResult(
        ok=False, values=None, error=outcome[:MAX_ERROR_CHARS], duration_ms=duration_ms
    )


# --- per-run directory ---------------------------------------------------------------------


def _work_root() -> Path:
    return Path(os.environ.get(WORK_DIR_ENV) or DEFAULT_WORK_ROOT).expanduser().resolve()


def _make_workdir() -> Path:
    root = _work_root()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    workdir = root / uuid.uuid4().hex
    workdir.mkdir(mode=0o700)
    (workdir / "in").mkdir(mode=0o755)
    out_dir = workdir / "out"
    out_dir.mkdir()
    out_dir.chmod(0o777)  # uid 10001 writes here; the 0o700 parents keep other host users out
    return workdir


def _write_inputs(in_dir: Path, code: str, values: pl.Series) -> None:
    """Write ``transform.py`` and ``values.parquet`` (one String column) into ``in_dir``."""
    in_dir.mkdir(exist_ok=True)
    code_path = in_dir / CODE_FILE
    code_path.write_text(code, encoding="utf-8")
    values_path = in_dir / VALUES_FILE
    pl.DataFrame({COLUMN: values}).write_parquet(values_path)
    for path in (code_path, values_path):
        path.chmod(0o644)


def _remove_workdir(path: Path) -> None:
    """Delete a per-run directory, including anything the container made unreadable."""

    def unlock_and_retry(func: Callable[..., Any], failed: str, exc: BaseException) -> None:
        for target in (os.path.dirname(failed), failed):
            with contextlib.suppress(OSError):
                if stat.S_ISDIR(os.lstat(target).st_mode):  # never chmod through a symlink
                    os.chmod(target, 0o700)
        if func in (os.rmdir, os.unlink, os.remove):
            func(failed)
        else:  # os.open / os.scandir / os.lstat on a locked directory
            shutil.rmtree(failed, onexc=unlock_and_retry)

    try:
        shutil.rmtree(path, onexc=unlock_and_retry)
    except FileNotFoundError:
        pass
    except OSError:
        log.exception("could not remove sandbox work dir %s", path)


# --- container -----------------------------------------------------------------------------


def _container_kwargs(workdir: Path, name: str) -> dict[str, Any]:
    mem = f"{SANDBOX_MEM_MB}m"
    return {
        "name": name,
        "labels": dict(CONTAINER_LABEL),
        "network_disabled": True,
        "network_mode": "none",
        "read_only": True,
        "user": "10001",
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "mem_limit": mem,
        "memswap_limit": mem,  # equal to mem_limit: no swap on top (memory.swap.max = 0)
        "pids_limit": SANDBOX_PIDS,
        "ulimits": [Ulimit(name="nofile", soft=NOFILE_LIMIT, hard=NOFILE_LIMIT)],
        "tmpfs": {"/tmp": TMPFS_OPTS},
        "environment": {},
        "volumes": {
            str(workdir / "in"): {"bind": IN_MOUNT, "mode": "ro"},
            str(workdir / "out"): {"bind": OUT_MOUNT, "mode": "rw"},
        },
        "log_config": LogConfig(
            type=LogConfig.types.JSON, config={"max-size": LOG_MAX_SIZE, "max-file": "1"}
        ),
        "detach": True,
    }


def _run_container(
    client: docker.DockerClient, workdir: Path, expected_len: int, timeout_s: int
) -> pl.Series | str:
    """Run the container; return the output series or the reason it failed."""
    name = f"sluice-sandbox-{workdir.name}"
    out_dir = workdir / "out"
    container: Any = None
    try:
        try:
            container = client.containers.run(SANDBOX_IMAGE, **_container_kwargs(workdir, name))
        except docker.errors.ImageNotFound as exc:
            raise SandboxError(
                f"sandbox image {SANDBOX_IMAGE} not found: run `make sandbox-image`"
            ) from exc
        exit_code, reason = _wait(container, out_dir, timeout_s)
        if reason is not None:
            return reason
        return _collect(container, out_dir, exit_code, expected_len)
    finally:
        _remove_container(client, container, name)


def _wait(container: Any, out_dir: Path, timeout_s: int) -> tuple[int, str | None]:
    """Wait for the container to stop; kill it on timeout or when its output grows too large."""
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill(container)
            return -1, f"timeout: transform did not finish within {timeout_s}s"
        try:
            status = container.wait(timeout=min(POLL_INTERVAL_S, remaining))
            return int(status.get("StatusCode", -1)), None
        except _STILL_RUNNING:
            pass
        if _out_size_exceeds(out_dir, SANDBOX_OUTPUT_MAX_BYTES):
            _kill(container)
            return -1, f"output exceeded the {SANDBOX_OUTPUT_MAX_BYTES} byte limit"


def _kill(container: Any) -> None:
    with contextlib.suppress(docker.errors.APIError):  # already exited: nothing to kill
        container.kill()


def _remove_container(client: docker.DockerClient, container: Any, name: str) -> None:
    try:
        if container is None:  # run() failed after create: find the container by its name
            container = client.containers.get(name)
        container.remove(force=True)
    except docker.errors.NotFound:
        pass
    except Exception:
        log.exception("could not remove sandbox container %s", name)


def _out_size_exceeds(out_dir: Path, limit: int) -> bool:
    total = 0
    entries = 0
    for dirpath, dirnames, filenames in os.walk(out_dir, followlinks=False):
        for entry in (*dirnames, *filenames):
            entries += 1
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(dirpath, entry)).st_size
            if total > limit or entries > MAX_OUT_ENTRIES:
                return True
    return False


# --- results -------------------------------------------------------------------------------


def _collect(container: Any, out_dir: Path, exit_code: int, expected_len: int) -> pl.Series | str:
    with contextlib.suppress(docker.errors.APIError):
        container.reload()
    state = (getattr(container, "attrs", None) or {}).get("State") or {}
    if state.get("OOMKilled"):
        return f"memory limit exceeded ({SANDBOX_MEM_MB} MiB): the container was OOM-killed"
    try:
        error_bytes = _read_regular(out_dir / ERROR_FILE, MAX_ERROR_CHARS * 4)
        if error_bytes is not None:
            return error_bytes.decode("utf-8", errors="replace")[:MAX_ERROR_CHARS]
        if exit_code != 0:
            return f"sandbox exited with exit code {exit_code}: {_logs_tail(container)}"
        return _read_output(out_dir / VALUES_FILE, expected_len)
    except _Rejected as exc:
        return str(exc)


def _logs_tail(container: Any) -> str:
    try:
        raw = container.logs(stdout=True, stderr=True, tail=20)
    except docker.errors.APIError:
        return "(no logs)"
    return raw[-MAX_ERROR_CHARS:].decode("utf-8", errors="replace")


def _read_regular(path: Path, limit: int, *, truncate: bool = True) -> bytes | None:
    """Read at most ``limit`` bytes of a regular file without following links or blocking.

    Returns ``None`` when the file does not exist. Raises :class:`_Rejected` for symlinks, FIFOs,
    devices and directories, and, unless ``truncate``, for files larger than ``limit``.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:  # ELOOP: a symlink
        raise _Rejected(f"{path.name} is not a regular file") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _Rejected(f"{path.name} is not a regular file")
        chunks: list[bytes] = []
        remaining = limit if truncate else limit + 1  # one extra byte detects "too large"
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if not truncate and len(data) > limit:
        raise _Rejected(f"output exceeded the {limit} byte limit")
    return data


def _read_output(path: Path, expected_len: int) -> pl.Series:
    """Validate and load ``values.parquet``: uncompressed, one String column, right length."""
    data = _read_regular(path, SANDBOX_OUTPUT_MAX_BYTES, truncate=False)
    if data is None:
        raise _Rejected("transform produced no output (values.parquet missing)")
    try:
        parquet = pq.ParquetFile(io.BytesIO(data), read_dictionary=[COLUMN])
        meta = parquet.metadata
        schema = parquet.schema_arrow
    except KeyError:  # read_dictionary names a column the file lacks
        names = pq.read_schema(io.BytesIO(data)).names
        raise _Rejected(f"output must have exactly one column {COLUMN!r}, got {names}") from None
    except (pa.ArrowException, OSError, ValueError) as exc:
        raise _Rejected(f"output is not a valid parquet file: {exc}") from exc
    if schema.names != [COLUMN]:
        raise _Rejected(f"output must have exactly one column {COLUMN!r}, got {schema.names}")
    if meta.num_rows != expected_len:
        raise _Rejected(f"output length {meta.num_rows} != input length {expected_len}")
    kind = schema.field(COLUMN).type
    if pa.types.is_dictionary(kind):
        kind = kind.value_type
    if not (pa.types.is_string(kind) or pa.types.is_large_string(kind)):
        raise _Rejected(f"output column must be String, got {kind}")
    for rg in range(meta.num_row_groups):
        codec = meta.row_group(rg).column(0).compression
        if codec != "UNCOMPRESSED":
            raise _Rejected(f"output parquet must be uncompressed, got {codec}")
    try:
        # A dictionary-encoded read keeps memory near the (capped) file size whatever encoding
        # the file uses, so the decoded size is checked before any string is materialised.
        column = parquet.read(columns=[COLUMN]).column(COLUMN)
        decoded = 0
        for chunk in column.chunks:
            lengths = pc.binary_length(chunk.dictionary)
            decoded += pc.sum(pc.take(lengths, chunk.indices)).as_py() or 0
            if decoded > SANDBOX_OUTPUT_MAX_BYTES:
                raise _Rejected(
                    f"decoded output exceeded the {SANDBOX_OUTPUT_MAX_BYTES} byte limit"
                )
        plain = column.cast(pa.large_string()).combine_chunks()
    except (pa.ArrowException, OSError, ValueError) as exc:
        raise _Rejected(f"output is not a valid parquet file: {exc}") from exc
    series = pl.Series(COLUMN, plain, dtype=pl.String)
    if series.len() != expected_len:
        raise _Rejected(f"output length {series.len()} != input length {expected_len}")
    return series
