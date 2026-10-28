"""Sandbox runner, host side, against a fake Docker client.

No container starts here and no generated code runs: the fake "container" plays the sandbox by
writing fixed files into the out mount from the test process.
"""

import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import docker.errors
import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import requests
import urllib3

from sluice.agent import caps
from sluice.sandbox import runner
from sluice.sandbox.runner import SandboxError, SandboxResult, run_transform

CODE = "def transform(values):\n    return values\n"
VALUES = pl.Series("values", ["1,24,500.50", " 7 ", None], dtype=pl.String)

WaitHook = Callable[[Path, Path], dict[str, Any]]


def _write_out(out_dir: Path, series: pl.Series) -> None:
    pl.DataFrame({"values": series}).write_parquet(
        out_dir / "values.parquet", compression="uncompressed"
    )


def _echo(in_dir: Path, out_dir: Path) -> dict[str, Any]:
    """Behave like a successful identity transform."""
    _write_out(out_dir, pl.read_parquet(in_dir / "values.parquet")["values"])
    return {"StatusCode": 0}


class FakeContainer:
    def __init__(
        self, kwargs: dict[str, Any], hook: WaitHook | None, wait_exc: BaseException | None
    ):
        self.kwargs = kwargs
        self.hook = hook
        self.wait_exc = wait_exc
        self.kills = 0
        self.removed: list[dict[str, Any]] = []
        self.attrs: dict[str, Any] = {"State": {"OOMKilled": False}}
        mounts = {v["bind"]: Path(k) for k, v in kwargs["volumes"].items()}
        self.in_dir = mounts["/work/in"]
        self.out_dir = mounts["/work/out"]

    def wait(self, timeout: float | None = None) -> dict[str, Any]:
        if self.wait_exc is not None:
            raise self.wait_exc
        assert self.hook is not None
        return self.hook(self.in_dir, self.out_dir)

    def kill(self) -> None:
        self.kills += 1

    def reload(self) -> None:
        pass

    def logs(self, **kwargs: Any) -> bytes:
        return b"stderr tail"

    def remove(self, **kwargs: Any) -> None:
        self.removed.append(kwargs)


class FakeContainers:
    def __init__(self, client: FakeClient) -> None:
        self.client = client

    def run(self, image: str, **kwargs: Any) -> FakeContainer:
        self.client.image = image
        self.client.run_kwargs = kwargs
        if self.client.run_exc is not None:
            raise self.client.run_exc
        self.client.container = FakeContainer(kwargs, self.client.hook, self.client.wait_exc)
        return self.client.container

    def get(self, name: str) -> FakeContainer:
        self.client.got.append(name)
        if self.client.orphan is None:
            raise docker.errors.NotFound("no such container")
        return self.client.orphan


class FakeClient:
    def __init__(
        self,
        hook: WaitHook | None = _echo,
        *,
        wait_exc: BaseException | None = None,
        run_exc: BaseException | None = None,
    ) -> None:
        self.hook = hook
        self.wait_exc = wait_exc
        self.run_exc = run_exc
        self.image = ""
        self.run_kwargs: dict[str, Any] = {}
        self.container: FakeContainer | None = None
        self.orphan: FakeContainer | None = None
        self.got: list[str] = []
        self.containers = FakeContainers(self)


@pytest.fixture(autouse=True)
def work_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The runner-owned directory inside ``$SLUICE_SANDBOX_DIR``."""
    base = tmp_path / "sandbox-base"
    monkeypatch.setenv(runner.WORK_DIR_ENV, str(base))
    monkeypatch.setattr(runner, "POLL_INTERVAL_S", 0.01)
    return base / runner.RUNNER_SUBDIR


def _run(
    client: FakeClient, code: str = CODE, values: pl.Series = VALUES, **kw: Any
) -> SandboxResult:
    return run_transform(code, values, client=client, **kw)  # type: ignore[arg-type]


def _assert_cleaned(client: FakeClient, root: Path) -> None:
    assert client.container is not None
    assert client.container.removed == [{"force": True}]
    assert list(root.iterdir()) == []


# --- happy path and the container contract -------------------------------------------------


def test_run_transform_identity_returns_values(work_root: Path) -> None:
    client = FakeClient()
    result = _run(client)
    assert result.ok is True
    assert result.error is None
    assert result.values is not None
    assert result.values.to_list() == VALUES.to_list()
    assert result.values.dtype == pl.String
    assert result.duration_ms >= 0
    _assert_cleaned(client, work_root)


def test_run_transform_passes_hardening_flags() -> None:
    client = FakeClient()
    _run(client)
    kw = client.run_kwargs
    assert client.image == runner.SANDBOX_IMAGE == "sluice-sandbox:dev"
    assert kw["network_disabled"] is True
    assert kw["network_mode"] == "none"
    assert kw["read_only"] is True
    assert kw["user"] == "10001"
    assert kw["cap_drop"] == ["ALL"]
    assert kw["security_opt"] == ["no-new-privileges"]
    assert kw["mem_limit"] == f"{caps.SANDBOX_MEM_MB}m"
    assert kw["memswap_limit"] == f"{caps.SANDBOX_MEM_MB}m"
    assert kw["pids_limit"] == caps.SANDBOX_PIDS
    assert kw["tmpfs"] == {"/tmp": "size=64m,noexec,nosuid,nodev"}
    assert kw["environment"] == {}
    assert kw["detach"] is True
    assert kw["nano_cpus"] == int(caps.SANDBOX_CPUS * 1_000_000_000) == 1_000_000_000
    assert kw["name"].startswith("sluice-sandbox-")
    assert kw["labels"] == {"sluice.sandbox": "1"}
    assert [(u.name, u.soft, u.hard) for u in kw["ulimits"]] == [("nofile", 256, 256)]
    assert kw["log_config"].type == "json-file"
    modes = {v["bind"]: v["mode"] for v in kw["volumes"].values()}
    assert modes == {"/work/in": "ro", "/work/out": "rw"}


def test_run_transform_writes_code_and_input(work_root: Path) -> None:
    seen: dict[str, Any] = {}

    def hook(in_dir: Path, out_dir: Path) -> dict[str, Any]:
        seen["code"] = (in_dir / "transform.py").read_text(encoding="utf-8")
        frame = pl.read_parquet(in_dir / "values.parquet")
        seen["columns"] = frame.columns
        seen["values"] = frame["values"].to_list()
        seen["work_root"] = in_dir.parent.parent
        return _echo(in_dir, out_dir)

    _run(FakeClient(hook))
    assert seen == {
        "code": CODE,
        "columns": ["values"],
        "values": VALUES.to_list(),
        "work_root": work_root,
    }


def test_run_transform_rejects_non_string_input() -> None:
    with pytest.raises(TypeError, match="String"):
        _run(FakeClient(), values=pl.Series("values", [1, 2]))


# --- cleanup in every path -----------------------------------------------------------------


def test_container_removed_when_wait_raises(work_root: Path) -> None:
    client = FakeClient(wait_exc=docker.errors.APIError("daemon hiccup"))
    with pytest.raises(docker.errors.APIError):
        _run(client)
    _assert_cleaned(client, work_root)


def test_container_removed_on_keyboard_interrupt_in_wait(work_root: Path) -> None:
    client = FakeClient(wait_exc=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        _run(client)
    _assert_cleaned(client, work_root)


def test_container_removed_by_name_when_run_raises(work_root: Path) -> None:
    client = FakeClient(run_exc=docker.errors.APIError("start failed"))
    client.orphan = FakeContainer(
        {"volumes": {"/x": {"bind": "/work/in"}, "/y": {"bind": "/work/out"}}}, None, None
    )
    with pytest.raises(docker.errors.APIError):
        _run(client)
    assert client.got == [client.run_kwargs["name"]]
    assert client.orphan.removed == [{"force": True}]
    assert list(work_root.iterdir()) == []


def test_missing_image_raises_sandbox_error(work_root: Path) -> None:
    client = FakeClient(run_exc=docker.errors.ImageNotFound("nope"))
    with pytest.raises(SandboxError, match="make sandbox-image"):
        _run(client)
    assert list(work_root.iterdir()) == []


def test_workdir_removed_even_with_unwritable_dirs(work_root: Path) -> None:
    def hook(in_dir: Path, out_dir: Path) -> dict[str, Any]:
        locked = out_dir / "locked"
        locked.mkdir()
        (locked / "f").write_text("x")
        locked.chmod(0)
        out_dir.chmod(0o500)
        return {"StatusCode": 1}

    _run(FakeClient(hook))
    assert list(work_root.iterdir()) == []


# --- failures the transform is blamed for -------------------------------------------------


def test_timeout_kills_container_and_reports_timeout(work_root: Path) -> None:
    client = FakeClient(wait_exc=requests.exceptions.ReadTimeout("still running"))
    result = _run(client, timeout_s=1)
    assert result.ok is False
    assert result.values is None
    assert result.error is not None and "timeout" in result.error
    assert client.container is not None and client.container.kills >= 1
    assert 1000 <= result.duration_ms < 5000
    _assert_cleaned(client, work_root)


def test_unix_socket_read_timeout_counts_as_still_running() -> None:
    # What docker-py really raises for a wait timeout over a unix socket.
    read_timeout = urllib3.exceptions.ReadTimeoutError(None, None, "Read timed out.")  # type: ignore[arg-type]
    client = FakeClient(wait_exc=requests.exceptions.ConnectionError(read_timeout))
    result = _run(client, timeout_s=1)
    assert result.ok is False and result.error is not None and "timeout" in result.error


def test_output_growth_past_cap_kills_container(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "SANDBOX_OUTPUT_MAX_BYTES", 1_000)
    client = FakeClient(wait_exc=requests.exceptions.ReadTimeout("running"))
    original_run = client.containers.run

    def run(image: str, **kwargs: Any) -> FakeContainer:
        container = original_run(image, **kwargs)
        (container.out_dir / "junk").write_bytes(b"x" * 5_000)
        return container

    client.containers.run = run  # type: ignore[method-assign]
    result = _run(client, timeout_s=10)
    assert result.ok is False
    assert result.error is not None and "output" in result.error and "limit" in result.error
    assert client.container is not None and client.container.kills >= 1
    assert result.duration_ms < 5000


def test_length_mismatch_fails() -> None:
    def hook(in_dir: Path, out_dir: Path) -> dict[str, Any]:
        _write_out(out_dir, pl.Series(["a"], dtype=pl.String))
        return {"StatusCode": 0}

    result = _run(FakeClient(hook))
    assert result.ok is False
    assert result.values is None
    assert result.error is not None and "length" in result.error


def test_extra_column_fails() -> None:
    def hook(in_dir: Path, out_dir: Path) -> dict[str, Any]:
        pl.DataFrame({"values": ["a", "b", "c"], "x": ["1", "2", "3"]}).write_parquet(
            out_dir / "values.parquet", compression="uncompressed"
        )
        return {"StatusCode": 0}

    result = _run(FakeClient(hook))
    assert result.ok is False and result.error is not None and "column" in result.error


def test_wrong_column_name_fails() -> None:
    def hook(in_dir: Path, out_dir: Path) -> dict[str, Any]:
        pl.DataFrame({"other": ["a", "b", "c"]}).write_parquet(
            out_dir / "values.parquet", compression="uncompressed"
        )
        return {"StatusCode": 0}

    result = _run(FakeClient(hook))
    assert result.ok is False and result.error is not None and "column" in result.error


def test_non_string_output_fails() -> None:
    def hook(in_dir: Path, out_dir: Path) -> dict[str, Any]:
        pl.DataFrame({"values": [1, 2, 3]}).write_parquet(
            out_dir / "values.parquet", compression="uncompressed"
        )
        return {"StatusCode": 0}

    result = _run(FakeClient(hook))
    assert result.ok is False and result.error is not None and "String" in result.error


def test_missing_output_fails() -> None:
    result = _run(FakeClient(lambda i, o: {"StatusCode": 0}))
    assert result.ok is False and result.error is not None and "no output" in result.error


def test_garbage_output_fails() -> None:
    def hook(in_dir: Path, out_dir: Path) -> dict[str, Any]:
        (out_dir / "values.parquet").write_bytes(b"not parquet at all")
        return {"StatusCode": 0}

    result = _run(FakeClient(hook))
    assert result.ok is False and result.error is not None and "parquet" in result.error


def test_compressed_output_rejected() -> None:
    def hook(in_dir: Path, out_dir: Path) -> dict[str, Any]:
        pl.DataFrame({"values": ["a", "b", "c"]}).write_parquet(
            out_dir / "values.parquet", compression="zstd"
        )
        return {"StatusCode": 0}

    result = _run(FakeClient(hook))
    assert result.ok is False and result.error is not None and "uncompressed" in result.error


def test_dictionary_bomb_rejected_before_materialising(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "SANDBOX_OUTPUT_MAX_BYTES", 2_000_000)
    rows = 3

    def hook(in_dir: Path, out_dir: Path) -> dict[str, Any]:
        # One 1 MB dictionary entry referenced by every row: small file, large decoded column.
        big = pa.DictionaryArray.from_arrays(
            pa.array([0] * rows, type=pa.int32()), pa.array(["x" * 1_000_000])
        )
        pq.write_table(pa.table({"values": big}), out_dir / "values.parquet", compression="NONE")
        return {"StatusCode": 0}

    result = _run(FakeClient(hook))
    assert result.ok is False
    assert result.error is not None and "decoded" in result.error


def test_output_file_over_cap_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "SANDBOX_OUTPUT_MAX_BYTES", 100)
    result = _run(FakeClient())
    assert result.ok is False and result.error is not None and "limit" in result.error


def test_symlinked_output_is_not_followed(tmp_path: Path) -> None:
    secret = tmp_path / "host-secret.parquet"
    _write_out(tmp_path, VALUES)
    (tmp_path / "values.parquet").rename(secret)

    def hook(in_dir: Path, out_dir: Path) -> dict[str, Any]:
        (out_dir / "values.parquet").symlink_to(secret)
        (out_dir / "error.txt").symlink_to(secret)
        return {"StatusCode": 0}

    result = _run(FakeClient(hook))
    assert result.ok is False and result.error is not None and "regular file" in result.error
    assert secret.exists()


def test_fifo_output_does_not_block() -> None:
    def hook(in_dir: Path, out_dir: Path) -> dict[str, Any]:
        os.mkfifo(out_dir / "values.parquet")
        return {"StatusCode": 0}

    result = _run(FakeClient(hook))
    assert result.ok is False and result.error is not None and "regular file" in result.error


def test_nonzero_exit_reports_error_file_truncated() -> None:
    def hook(in_dir: Path, out_dir: Path) -> dict[str, Any]:
        (out_dir / "error.txt").write_text("ValueError: boom\n" + "y" * 10_000, encoding="utf-8")
        return {"StatusCode": 1}

    result = _run(FakeClient(hook))
    assert result.ok is False
    assert result.error is not None
    assert result.error.startswith("ValueError: boom")
    assert len(result.error) <= caps.MAX_ERROR_CHARS


def test_nonzero_exit_without_error_file_uses_logs() -> None:
    result = _run(FakeClient(lambda i, o: {"StatusCode": 3}))
    assert result.ok is False
    assert result.error is not None and "exit code 3" in result.error
    assert "stderr tail" in result.error


def test_error_file_wins_over_output_even_on_zero_exit() -> None:
    def hook(in_dir: Path, out_dir: Path) -> dict[str, Any]:
        _echo(in_dir, out_dir)
        (out_dir / "error.txt").write_text("late failure", encoding="utf-8")
        return {"StatusCode": 0}

    result = _run(FakeClient(hook))
    assert result.ok is False and result.error == "late failure"


def test_oom_kill_reported_as_memory_limit() -> None:
    client = FakeClient(lambda i, o: {"StatusCode": 137})
    original_run = client.containers.run

    def run(image: str, **kwargs: Any) -> FakeContainer:
        container = original_run(image, **kwargs)
        container.attrs = {"State": {"OOMKilled": True}}
        return container

    client.containers.run = run  # type: ignore[method-assign]
    result = _run(client)
    assert result.ok is False and result.error is not None and "memory" in result.error


# --- daemon discovery ----------------------------------------------------------------------


def test_get_docker_client_falls_back_to_current_context(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[str] = []

    class Pingable:
        def __init__(self, base_url: str, timeout: int) -> None:
            made.append(base_url)

        def ping(self) -> bool:
            return True

    class Ctx:
        Host = "unix:///home/u/.colima/default/docker.sock"

    def broken(**kwargs: Any) -> None:
        raise docker.errors.DockerException("no DOCKER_HOST socket")

    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(runner.docker, "from_env", broken)
    monkeypatch.setattr(runner.docker, "DockerClient", Pingable)
    monkeypatch.setattr(runner.ContextAPI, "get_current_context", staticmethod(lambda: Ctx()))
    client = runner.get_docker_client()
    assert isinstance(client, Pingable)
    assert made == [Ctx.Host]


def test_get_docker_client_raises_sandbox_error_when_no_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(**kwargs: Any) -> None:
        raise docker.errors.DockerException("down")

    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(runner.docker, "from_env", broken)
    monkeypatch.setattr(runner.ContextAPI, "get_current_context", staticmethod(lambda: None))
    with pytest.raises(SandboxError, match="Docker"):
        runner.get_docker_client()


def test_get_docker_client_explicit_docker_host_unreachable_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = "tcp://user:s3cret@10.0.0.9:2376"
    contexts: list[bool] = []

    def broken(**kwargs: Any) -> None:
        raise docker.errors.DockerException(f"Error connecting to {host}")

    def context() -> Any:
        contexts.append(True)
        return type("Ctx", (), {"Host": "unix:///colima.sock"})()

    monkeypatch.setenv("DOCKER_HOST", host)
    monkeypatch.setattr(runner.docker, "from_env", broken)
    monkeypatch.setattr(runner.ContextAPI, "get_current_context", staticmethod(context))
    with pytest.raises(SandboxError, match="DOCKER_HOST") as excinfo:
        runner.get_docker_client()
    assert contexts == []
    assert "s3cret" not in str(excinfo.value)


def test_get_docker_client_explicit_docker_host_reachable_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Pingable:
        def ping(self) -> bool:
            return True

    monkeypatch.setenv("DOCKER_HOST", "unix:///custom/docker.sock")
    made = Pingable()
    monkeypatch.setattr(runner.docker, "from_env", lambda **kwargs: made)
    monkeypatch.setattr(
        runner.ContextAPI, "get_current_context", staticmethod(lambda: pytest.fail("no fallback"))
    )
    assert runner.get_docker_client() is made


# --- image requirements come from uv.lock --------------------------------------------------


def test_locked_requirements_pin_polars_from_uv_lock() -> None:
    import tomllib

    from sluice.sandbox.image import locked_requirements

    lock_path = Path(__file__).resolve().parents[2] / "uv.lock"
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    versions = {p["name"]: p["version"] for p in lock["package"]}
    text = locked_requirements(lock_path)
    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    pins = [line.split(" ")[0] for line in lines]
    assert pins[0] == f"polars=={versions['polars']}"
    assert {p.split("==")[0] for p in pins} <= {"polars", "polars-runtime-32"}
    assert all("--hash=sha256:" in line for line in lines)


def test_sandbox_dirs_are_private_to_owner(work_root: Path) -> None:
    modes: dict[str, int] = {}

    def hook(in_dir: Path, out_dir: Path) -> dict[str, Any]:
        modes["root"] = stat.S_IMODE(in_dir.parent.parent.stat().st_mode)
        modes["code"] = stat.S_IMODE((in_dir / "transform.py").stat().st_mode)
        return _echo(in_dir, out_dir)

    _run(FakeClient(hook))
    assert modes["root"] == 0o700
    assert modes["code"] & 0o022 == 0


def test_client_created_by_run_transform_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []

    class Closable(FakeClient):
        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(runner, "get_docker_client", lambda: Closable())
    result = run_transform(CODE, VALUES)
    assert result.ok and closed == [True]


def test_caller_client_is_left_open() -> None:
    class Closable(FakeClient):
        closed = False

        def close(self) -> None:
            self.closed = True

    client = Closable()
    _run(client)
    assert client.closed is False


# --- fix round 1: owned work dir, post-exit size check, unreachable daemon -----------------


def test_work_dir_env_is_not_chmodded_runner_uses_own_subdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "shared"
    base.mkdir(mode=0o755)
    base.chmod(0o755)
    monkeypatch.setenv(runner.WORK_DIR_ENV, str(base))
    seen: dict[str, Path] = {}

    def hook(in_dir: Path, out_dir: Path) -> dict[str, Any]:
        seen["root"] = in_dir.parent.parent
        return _echo(in_dir, out_dir)

    _run(FakeClient(hook))
    assert seen["root"] == base / runner.RUNNER_SUBDIR
    assert stat.S_IMODE(base.stat().st_mode) == 0o755
    assert stat.S_IMODE((base / runner.RUNNER_SUBDIR).stat().st_mode) == 0o700


def test_symlinked_runner_dir_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = tmp_path / "base"
    base.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (base / runner.RUNNER_SUBDIR).symlink_to(elsewhere)
    monkeypatch.setenv(runner.WORK_DIR_ENV, str(base))
    client = FakeClient()
    with pytest.raises(SandboxError, match="symlink"):
        _run(client)
    assert client.container is None
    assert list(elsewhere.iterdir()) == []


def test_runner_dir_owned_by_someone_else_is_refused(
    work_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_root.mkdir(parents=True)
    monkeypatch.setattr(runner.os, "getuid", lambda: os.stat(work_root).st_uid + 1)
    client = FakeClient()
    with pytest.raises(SandboxError, match="owned"):
        _run(client)
    assert client.container is None


def test_output_over_cap_after_exit_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A burst written between the last poll and exit is caught by a final size check."""
    monkeypatch.setattr(runner, "SANDBOX_OUTPUT_MAX_BYTES", 5_000)

    def hook(in_dir: Path, out_dir: Path) -> dict[str, Any]:
        _echo(in_dir, out_dir)
        (out_dir / "junk").write_bytes(b"x" * 10_000)
        return {"StatusCode": 0}

    result = _run(FakeClient(hook))
    assert result.ok is False
    assert result.error is not None and "output" in result.error and "limit" in result.error


def test_unreachable_daemon_while_waiting_raises_without_hot_loop(work_root: Path) -> None:
    client = FakeClient(wait_exc=requests.exceptions.ConnectionError("Connection refused"))
    calls = {"n": 0}
    original_run = client.containers.run

    def run(image: str, **kwargs: Any) -> FakeContainer:
        container = original_run(image, **kwargs)
        real_wait_exc = container.wait_exc

        def wait(timeout: float | None = None) -> dict[str, Any]:
            calls["n"] += 1
            assert real_wait_exc is not None
            raise real_wait_exc

        container.wait = wait  # type: ignore[method-assign]
        return container

    client.containers.run = run  # type: ignore[method-assign]
    with pytest.raises(SandboxError, match="unreachable"):
        _run(client, timeout_s=1)
    # POLL_INTERVAL_S is 0.01 here: sleeping between retries bounds this near 100.
    assert calls["n"] <= 150
    _assert_cleaned(client, work_root)


# --- fix round 2: fd-based work dir (symlink race, Linux portability) ----------------------


def test_symlink_swap_after_check_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swap ``sluice-sandbox`` for a symlink to an attacker tree after the dir was opened."""
    base = tmp_path / "base"
    monkeypatch.setenv(runner.WORK_DIR_ENV, str(base))
    moved = base / "moved"
    evil = tmp_path / "evil"
    real_write = runner._write_inputs

    def write_then_swap(*args: Any) -> None:
        real_write(*args)
        (base / runner.RUNNER_SUBDIR).rename(moved)
        (run_name,) = [p.name for p in moved.iterdir()]
        for sub in ("in", "out"):
            (evil / run_name / sub).mkdir(parents=True)
        (base / runner.RUNNER_SUBDIR).symlink_to(evil, target_is_directory=True)

    monkeypatch.setattr(runner, "_write_inputs", write_then_swap)
    client = FakeClient()
    with pytest.raises(SandboxError, match="changed"):
        _run(client)
    assert client.container is None  # nothing was mounted
    assert all(not any(d.iterdir()) for d in evil.glob("*/*"))  # attacker tree untouched
    assert list(moved.iterdir()) == []  # our real run dir was still removed, through its fd


def test_inputs_are_written_through_the_held_fd_not_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "base"
    monkeypatch.setenv(runner.WORK_DIR_ENV, str(base))
    real_make = runner._make_workdir
    evil = tmp_path / "evil"
    evil.mkdir()

    def make_then_swap() -> Any:
        work = real_make()
        (base / runner.RUNNER_SUBDIR).rename(base / "moved")
        (base / runner.RUNNER_SUBDIR).symlink_to(evil, target_is_directory=True)
        return work

    monkeypatch.setattr(runner, "_make_workdir", make_then_swap)
    with pytest.raises(SandboxError, match="changed"):
        _run(FakeClient())
    assert list(evil.iterdir()) == []


def test_no_chmod_without_following_symlinks(
    monkeypatch: pytest.MonkeyPatch, work_root: Path
) -> None:
    """Linux has no lchmod: ``os.chmod(..., follow_symlinks=False)`` raises NotImplementedError."""
    real_chmod = os.chmod

    def linux_chmod(*args: Any, follow_symlinks: bool = True, **kwargs: Any) -> None:
        if not follow_symlinks:
            raise NotImplementedError("chmod: follow_symlinks unavailable on this platform")
        real_chmod(*args, **kwargs)

    monkeypatch.setattr(runner.os, "chmod", linux_chmod)

    def lock_everything(in_dir: Path, out_dir: Path) -> dict[str, Any]:
        _echo(in_dir, out_dir)
        locked = out_dir / "locked"
        locked.mkdir()
        (locked / "f").write_text("x")
        locked.chmod(0)
        out_dir.chmod(0o500)
        return {"StatusCode": 1}

    _run(FakeClient(lock_everything))
    assert list(work_root.iterdir()) == []


def test_runner_source_never_chmods_with_follow_symlinks_false() -> None:
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(runner))
    bad = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", getattr(node.func, "id", "")) in {"chmod", "lchmod"}
        and any(
            kw.arg == "follow_symlinks"
            and not (isinstance(kw.value, ast.Constant) and kw.value.value is True)
            for kw in node.keywords
        )
    ]
    lchmod_uses = [
        n.lineno for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "lchmod"
    ]
    assert bad == [] and lchmod_uses == []


def test_locked_subdir_in_out_counts_as_over_the_limit() -> None:
    """A dir the host cannot list could hide a flood, so the watchdog kills the container."""
    client = FakeClient(wait_exc=requests.exceptions.ReadTimeout("running"))
    original_run = client.containers.run

    def run(image: str, **kwargs: Any) -> FakeContainer:
        container = original_run(image, **kwargs)
        hidden = container.out_dir / "hidden"
        hidden.mkdir()
        (hidden / "flood").write_bytes(b"x" * 100)
        hidden.chmod(0)
        return container

    client.containers.run = run  # type: ignore[method-assign]
    result = _run(client, timeout_s=10)
    assert result.ok is False
    assert result.error is not None and "limit" in result.error
    assert client.container is not None and client.container.kills >= 1
