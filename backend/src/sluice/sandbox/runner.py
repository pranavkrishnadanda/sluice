"""Run one generated ``transform(values) -> values`` inside the ``sluice-sandbox:dev`` container.

Generated code never runs in this process. :func:`run_transform` writes the code and the input
column to a fresh per-run directory, starts a locked-down container on it and reads back
``values.parquet`` (or ``error.txt``) once the container has stopped.

Container hardening (see :func:`_container_kwargs`): no network (``network_disabled`` plus
``network_mode="none"``, loopback only), read-only root filesystem, non-root uid 10001, every
capability dropped, ``no-new-privileges``, memory limit with swap equal to it (no swap spill),
one CPU, pids limit, ``nofile`` limit, size-capped ``noexec`` tmpfs at ``/tmp``, empty
environment, the input mount read-only and only the output mount writable, bounded container logs.

Host-side defences against a hostile container:

* wall-clock timeout, then ``kill``; the out mount is polled and the container killed once it
  holds more than :data:`SANDBOX_OUTPUT_MAX_BYTES` (no disk flooding through the bind mount),
  and checked once more after the container exits;
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

Docker daemon: :func:`get_docker_client` uses ``docker.from_env()``. An explicit ``DOCKER_HOST``
is final (unreachable means :class:`SandboxError`); only when it is unset does it fall back to the
host of the current Docker CLI context (what ``docker context inspect`` shows), which is how
Colima is found.

Per-run directories live in ``<$SLUICE_SANDBOX_DIR>/sluice-sandbox`` (default
``~/.cache/sluice/sluice-sandbox``), a 0700 directory the runner owns.
On Colima a bind-mounted directory must be inside a path shared into the VM; Colima shares
``$HOME`` but not ``/private/tmp``, which is why the system temp dir is not used.
"""

import contextlib
import io
import logging
import os
import stat
import time
import uuid
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
import urllib3
from docker.context import ContextAPI
from docker.types import LogConfig, Ulimit
from opentelemetry import trace

from sluice import tracing
from sluice.agent.caps import (
    MAX_ERROR_CHARS,
    SANDBOX_CPUS,
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

DEFAULT_WORK_ROOT: Final = Path("~/.cache/sluice")

RUNNER_SUBDIR: Final = "sluice-sandbox"
"""Directory inside the work root that the runner creates, owns and keeps at mode 0700."""

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


def _is_read_timeout(exc: requests.exceptions.ConnectionError) -> bool:
    """Over a unix socket, a ``wait(timeout=...)`` read timeout surfaces as a ConnectionError
    wrapping urllib3's ReadTimeoutError; any other ConnectionError means the daemon is gone."""
    return any(isinstance(arg, urllib3.exceptions.ReadTimeoutError) for arg in exc.args)


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
    """Connect to the Docker daemon.

    An explicit ``DOCKER_HOST`` is honoured and never second-guessed: if it is unreachable this
    raises rather than sending containers to another daemon. Only when ``DOCKER_HOST`` is unset
    does it try the default socket and then the host of the current Docker CLI context (how
    Colima is found).

    Raises:
        SandboxError: no daemon is reachable, or the current Docker context cannot be read or
            names an unusable host (the message never includes the host URL, which may carry
            credentials).
    """
    explicit = bool(os.environ.get("DOCKER_HOST"))
    try:
        client = docker.from_env(timeout=DOCKER_API_TIMEOUT_S)
        client.ping()
        return client
    except docker.errors.DockerException as env_error:
        if explicit:
            raise SandboxError(
                "DOCKER_HOST is set but no Docker daemon is reachable there "
                f"({type(env_error).__name__}); fix or unset DOCKER_HOST"
            ) from None
        try:
            context = ContextAPI.get_current_context()
        except (docker.errors.DockerException, OSError, ValueError) as ctx_error:
            # A broken context store must not crash the run: it is "no sandbox", not a bug.
            raise SandboxError(
                f"Docker daemon not reachable ({type(env_error).__name__}) and the current "
                f"Docker context cannot be read ({type(ctx_error).__name__})"
            ) from None
        host = getattr(context, "Host", None)
        if not host:
            raise SandboxError(
                f"Docker daemon not reachable ({type(env_error).__name__}) and the current "
                "Docker context names no host"
            ) from None
        try:
            client = docker.DockerClient(base_url=host, timeout=DOCKER_API_TIMEOUT_S)
            client.ping()
            return client
        except (docker.errors.DockerException, OSError, ValueError) as ctx_error:
            raise SandboxError(
                "Docker daemon of the current Docker context is not reachable "
                f"({type(ctx_error).__name__})"
            ) from None


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
    # `_run_container` / `_wait` add sluice.exit_code and set sluice.timed_out on this span.
    with tracing.span("sluice.sandbox", {"sluice.timed_out": False}) as current:
        start = time.monotonic()
        docker_client = client if client is not None else get_docker_client()
        try:
            work = _make_workdir()
            try:
                _write_inputs(work.in_fd, code, values)
                outcome = _run_container(docker_client, work, len(values), timeout_s)
            finally:
                _remove_workdir(work)
        finally:
            if client is None:  # opened here, so closed here: no socket leak across many runs
                docker_client.close()
        duration_ms = int((time.monotonic() - start) * 1000)
        current.set_attribute("sluice.duration_ms", duration_ms)
    if isinstance(outcome, pl.Series):
        return SandboxResult(ok=True, values=outcome, error=None, duration_ms=duration_ms)
    return SandboxResult(
        ok=False, values=None, error=outcome[:MAX_ERROR_CHARS], duration_ms=duration_ms
    )


# --- per-run directory ---------------------------------------------------------------------
#
# Everything below the configured base directory is handled through directory file descriptors
# opened with O_NOFOLLOW, so swapping a path component for a symlink after a check cannot
# redirect a write, a read or a removal. The bind-mount paths are the one place Docker needs
# strings; they are checked against the open descriptors (same device and inode) immediately
# before the container starts. Only portable calls are used (fchmod, *at with dir_fd); there is
# no ``chmod(..., follow_symlinks=False)``, which is not portable (NotImplementedError on Linux).

_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@dataclass(slots=True)
class _WorkDir:
    """A per-run directory, held open: ``path`` is only used for bind mounts and messages."""

    path: Path
    root_fd: int
    run_fd: int
    in_fd: int
    out_fd: int

    @property
    def name(self) -> str:
        return self.path.name

    def close(self) -> None:
        for fd in (self.in_fd, self.out_fd, self.run_fd, self.root_fd):
            with contextlib.suppress(OSError):
                os.close(fd)


def _open_owned_dir(name: str, parent_fd: int, shown: Path) -> int:
    """Open ``name`` under ``parent_fd`` without following links; it must be our directory."""
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:  # ELOOP (symlink) or ENOTDIR
        raise SandboxError(
            f"sandbox work dir {shown} is a symlink or not a directory; refusing to use it"
        ) from exc
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        raise SandboxError(f"sandbox work dir {shown} is not a directory owned by the current user")
    return fd


def _make_subdir(name: str, mode: int, parent_fd: int, shown: Path) -> int:
    os.mkdir(name, 0o700, dir_fd=parent_fd)
    fd = _open_owned_dir(name, parent_fd, shown)
    os.fchmod(fd, mode)  # explicit: mkdir's mode is filtered by the umask
    return fd


def _make_workdir() -> _WorkDir:
    """Create ``<$SLUICE_SANDBOX_DIR>/sluice-sandbox/<uuid>/{in,out}`` and hold it open.

    The configured directory is created if missing but never chmodded. ``sluice-sandbox`` is
    refused if it is a symlink, not a directory, or owned by another user; it is kept at 0700.
    """
    base = Path(os.environ.get(WORK_DIR_ENV) or DEFAULT_WORK_ROOT).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    root = base / RUNNER_SUBDIR
    base_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with contextlib.suppress(FileExistsError):
            os.mkdir(RUNNER_SUBDIR, 0o700, dir_fd=base_fd)
        root_fd = _open_owned_dir(RUNNER_SUBDIR, base_fd, root)
    finally:
        os.close(base_fd)
    fds = [root_fd]
    try:
        os.fchmod(root_fd, 0o700)
        name = uuid.uuid4().hex
        run_fd = _make_subdir(name, 0o700, root_fd, root / name)
        fds.append(run_fd)
        in_fd = _make_subdir("in", 0o755, run_fd, root / name / "in")
        fds.append(in_fd)
        # uid 10001 writes here; the 0o700 parents keep other host users out.
        out_fd = _make_subdir("out", 0o777, run_fd, root / name / "out")
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise
    return _WorkDir(root / name, root_fd, run_fd, in_fd, out_fd)


def _write_file_at(dir_fd: int, name: str, data: bytes) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dir_fd)
    try:
        os.fchmod(fd, 0o644)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    finally:
        os.close(fd)


def _write_inputs(in_fd: int, code: str, values: pl.Series) -> None:
    """Write ``transform.py`` and ``values.parquet`` (one String column) into the ``in`` dir."""
    _write_file_at(in_fd, CODE_FILE, code.encode("utf-8"))
    buffer = io.BytesIO()
    pl.DataFrame({COLUMN: values}).write_parquet(buffer)
    _write_file_at(in_fd, VALUES_FILE, buffer.getvalue())


def _verify_mount_paths(work: _WorkDir) -> None:
    """The bind-mount path strings must still name the directories we hold open."""
    for sub, fd in (("in", work.in_fd), ("out", work.out_fd)):
        held = os.fstat(fd)
        try:
            seen = os.stat(work.path / sub)
        except OSError:
            seen = None
        if seen is None or (seen.st_dev, seen.st_ino) != (held.st_dev, held.st_ino):
            raise SandboxError(f"sandbox work dir {work.path} changed under us; refusing to run")


def _rmtree_at(parent_fd: int, name: str) -> None:
    """Remove ``name`` under ``parent_fd`` without following symlinks, unlocking dirs as needed.

    Runs after the container is gone, so nothing races the lstat → chmod of a locked directory.
    """
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except PermissionError:  # the container made it mode 000: it is a real dir (lstat above)
        os.chmod(name, 0o700, dir_fd=parent_fd)
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    try:
        os.fchmod(fd, 0o700)  # write permission to unlink the entries
        for entry in os.listdir(fd):
            _rmtree_at(fd, entry)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def _remove_workdir(work: _WorkDir) -> None:
    """Delete a per-run directory (fd-relative) and close its descriptors."""
    try:
        _rmtree_at(work.root_fd, work.name)
    except FileNotFoundError:
        pass
    except OSError:
        log.exception("could not remove sandbox work dir %s", work.path)
    finally:
        work.close()


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
        "nano_cpus": int(SANDBOX_CPUS * 1_000_000_000),
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
    client: docker.DockerClient, work: _WorkDir, expected_len: int, timeout_s: int
) -> pl.Series | str:
    """Run the container; return the output series or the reason it failed."""
    name = f"sluice-sandbox-{work.name}"
    out_fd = work.out_fd
    container: Any = None
    try:
        _verify_mount_paths(work)
        try:
            container = client.containers.run(SANDBOX_IMAGE, **_container_kwargs(work.path, name))
        except docker.errors.ImageNotFound as exc:
            raise SandboxError(
                f"sandbox image {SANDBOX_IMAGE} not found: run `make sandbox-image`"
            ) from exc
        exit_code, reason = _wait(container, out_fd, timeout_s)
        trace.get_current_span().set_attribute("sluice.exit_code", exit_code)
        if reason is not None:
            return reason
        if _out_size_exceeds(out_fd, SANDBOX_OUTPUT_MAX_BYTES):  # written after the last poll
            return f"output exceeded the {SANDBOX_OUTPUT_MAX_BYTES} byte limit"
        return _collect(container, out_fd, exit_code, expected_len)
    finally:
        _remove_container(client, container, name)


def _wait(container: Any, out_fd: int, timeout_s: int) -> tuple[int, str | None]:
    """Wait for the container to stop; kill it on timeout or when its output grows too large."""
    deadline = time.monotonic() + timeout_s
    unreachable = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if unreachable:
                with contextlib.suppress(Exception):
                    container.kill()
                raise SandboxError(
                    f"Docker daemon unreachable while waiting for the sandbox ({timeout_s}s)"
                )
            _kill(container)
            trace.get_current_span().set_attribute("sluice.timed_out", True)
            return -1, f"timeout: transform did not finish within {timeout_s}s"
        try:
            status = container.wait(timeout=min(POLL_INTERVAL_S, remaining))
            return int(status.get("StatusCode", -1)), None
        except requests.exceptions.ReadTimeout:
            unreachable = False
        except requests.exceptions.ConnectionError as exc:
            unreachable = not _is_read_timeout(exc)
            if unreachable:  # refused at once: back off instead of spinning
                time.sleep(min(POLL_INTERVAL_S, max(deadline - time.monotonic(), 0)))
        if _out_size_exceeds(out_fd, SANDBOX_OUTPUT_MAX_BYTES):
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


def _out_size_exceeds(out_fd: int, limit: int) -> bool:
    """Total size of the out dir, fd-relative and without following links.

    A subdirectory the host cannot list (the container locked it) could hide any amount of
    data, so it counts as exceeding the limit.
    """
    unreadable: list[OSError] = []
    total = 0
    entries = 0
    walk = os.fwalk(".", dir_fd=out_fd, follow_symlinks=False, onerror=unreadable.append)
    for _, dirnames, filenames, dir_fd in walk:
        for entry in (*dirnames, *filenames):
            entries += 1
            with contextlib.suppress(OSError):
                total += os.stat(entry, dir_fd=dir_fd, follow_symlinks=False).st_size
            if total > limit or entries > MAX_OUT_ENTRIES:
                return True
    return bool(unreadable)


# --- results -------------------------------------------------------------------------------


def _collect(container: Any, out_fd: int, exit_code: int, expected_len: int) -> pl.Series | str:
    with contextlib.suppress(docker.errors.APIError):
        container.reload()
    state = (getattr(container, "attrs", None) or {}).get("State") or {}
    if state.get("OOMKilled"):
        return f"memory limit exceeded ({SANDBOX_MEM_MB} MiB): the container was OOM-killed"
    try:
        error_bytes = _read_regular(out_fd, ERROR_FILE, MAX_ERROR_CHARS * 4)
        if error_bytes is not None:
            return error_bytes.decode("utf-8", errors="replace")[:MAX_ERROR_CHARS]
        if exit_code != 0:
            return f"sandbox exited with exit code {exit_code}: {_logs_tail(container)}"
        return _read_output(out_fd, expected_len)
    except _Rejected as exc:
        return str(exc)


def _logs_tail(container: Any) -> str:
    try:
        raw = container.logs(stdout=True, stderr=True, tail=20)
    except docker.errors.APIError:
        return "(no logs)"
    return raw[-MAX_ERROR_CHARS:].decode("utf-8", errors="replace")


def _read_regular(dir_fd: int, name: str, limit: int, *, truncate: bool = True) -> bytes | None:
    """Read at most ``limit`` bytes of a regular file without following links or blocking.

    Returns ``None`` when the file does not exist. Raises :class:`_Rejected` for symlinks, FIFOs,
    devices and directories, and, unless ``truncate``, for files larger than ``limit``.
    """
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:  # ELOOP: a symlink
        raise _Rejected(f"{name} is not a regular file") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _Rejected(f"{name} is not a regular file")
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


def _read_output(out_fd: int, expected_len: int) -> pl.Series:
    """Validate and load ``values.parquet``: uncompressed, one String column, right length."""
    data = _read_regular(out_fd, VALUES_FILE, SANDBOX_OUTPUT_MAX_BYTES, truncate=False)
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
