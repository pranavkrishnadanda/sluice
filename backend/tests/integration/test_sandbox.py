"""The sandbox against a real Docker daemon and the ``sluice-sandbox:dev`` image.

Needs Docker (Colima) and ``make sandbox-image``; run with ``make test-integration``. Every
hostile transform below runs only inside the container. Each test checks two things: the attempt
is contained (it fails, or its probe shows the action was refused), and the host is untouched.
"""

import contextlib
import hashlib
import json
import shutil
import socket
import uuid
from collections.abc import Iterator
from pathlib import Path

import docker
import polars as pl
import pytest

from sluice.agent import caps
from sluice.sandbox import runner
from sluice.sandbox.runner import run_transform

pytestmark = pytest.mark.integration

VALUES = pl.Series("values", ["1,24,500.50", "  42 ", None, "7"], dtype=pl.String)
FAKE_GROQ = "gsk_canary_" + uuid.uuid4().hex
FAKE_GEMINI = "AIza_canary_" + uuid.uuid4().hex
IT_ROOT = Path.home() / ".cache" / "sluice" / "sandbox-it"  # under $HOME: shared into Colima


@pytest.fixture(scope="module")
def client() -> Iterator[docker.DockerClient]:
    c = runner.get_docker_client()
    try:
        c.images.get(runner.SANDBOX_IMAGE)
    except docker.errors.ImageNotFound:
        pytest.fail(f"{runner.SANDBOX_IMAGE} is missing: run `make sandbox-image`")
    yield c
    c.close()
    with contextlib.suppress(OSError):
        IT_ROOT.rmdir()


@pytest.fixture(autouse=True)
def isolated(
    client: docker.DockerClient, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, dict[str, str]]]:
    """Private work root under $HOME (Colima shares $HOME), canary secrets, in-dir snapshots.

    After each test: no sandbox container is left and the work root is empty.
    """
    root = IT_ROOT / uuid.uuid4().hex
    monkeypatch.setenv(runner.WORK_DIR_ENV, str(root))
    monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ)
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_GEMINI)

    snapshots: dict[str, dict[str, str]] = {}
    real_remove = runner._remove_workdir

    def snapshot_then_remove(path: Path) -> None:
        in_dir = path / "in"
        snapshots["in"] = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(in_dir.iterdir())
            if p.is_file()
        } | {p.name: "<dir>" for p in in_dir.iterdir() if not p.is_file()}
        real_remove(path)

    monkeypatch.setattr(runner, "_remove_workdir", snapshot_then_remove)
    yield snapshots
    leftover = client.containers.list(all=True, filters={"label": "sluice.sandbox=1"})
    assert leftover == [], "sandbox container leaked"
    runner_dir = root / runner.RUNNER_SUBDIR
    assert not runner_dir.exists() or list(runner_dir.iterdir()) == [], "work dir leaked"
    shutil.rmtree(root, ignore_errors=True)


def _probe(body: str) -> str:
    """Code whose transform returns one probe string per row (the probe runs in the container)."""
    return (
        "import polars as pl\n\n"
        "def transform(values):\n"
        + "".join(f"    {line}\n" for line in body.strip().splitlines())
        + "    return pl.Series([str(result)] * len(values), dtype=pl.String)\n"
    )


def _in_hashes(code: str) -> dict[str, str]:
    """What the host wrote into the in-mount: the code and the input parquet, nothing else."""
    tmp = IT_ROOT / ("ref-" + uuid.uuid4().hex)
    tmp.mkdir(parents=True)
    try:
        runner._write_inputs(tmp, code, VALUES)
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(tmp.iterdir())}
    finally:
        shutil.rmtree(tmp)


# --- it works ------------------------------------------------------------------------------


def test_identity_round_trip() -> None:
    result = run_transform("def transform(values):\n    return values\n", VALUES)
    assert result.ok, result.error
    assert result.values is not None
    assert result.values.to_list() == VALUES.to_list()
    assert result.values.dtype == pl.String


def test_indian_grouping_is_rewritten() -> None:
    code = (
        "import polars as pl\n\n"
        "def transform(values: pl.Series) -> pl.Series:\n"
        "    return values.str.strip_chars().str.replace_all(',', '')\n"
    )
    result = run_transform(code, VALUES)
    assert result.ok, result.error
    assert result.values is not None
    assert result.values.to_list() == ["124500.50", "42", None, "7"]


def test_container_identity_and_limits() -> None:
    """Non-root, no capabilities, no-new-privs, seccomp, cgroup memory/swap/pids/cpu limits."""
    code = _probe(
        """
from pathlib import Path
status = dict(
    line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines()
)
cg = Path("/sys/fs/cgroup")
def read(name):
    try:
        return (cg / name).read_text().strip()
    except OSError as exc:
        return f"err:{exc.errno}"
result = __import__("json").dumps({
    "uid": status["Uid"].split()[0],
    "cap_eff": status["CapEff"].strip(),
    "no_new_privs": status["NoNewPrivs"].strip(),
    "seccomp": status["Seccomp"].strip(),
    "mem_max": read("memory.max"),
    "swap_max": read("memory.swap.max"),
    "pids_max": read("pids.max"),
    "cpu_max": read("cpu.max"),
    "nofile": __import__("resource").getrlimit(__import__("resource").RLIMIT_NOFILE),
})
"""
    )
    result = run_transform(code, VALUES)
    assert result.ok, result.error
    assert result.values is not None
    probe = json.loads(result.values[0])
    assert probe["uid"] == "10001"
    assert probe["cap_eff"] == "0000000000000000"
    assert probe["no_new_privs"] == "1"
    assert probe["seccomp"] == "2"
    assert probe["mem_max"] == str(caps.SANDBOX_MEM_MB * 1024 * 1024)
    assert probe["swap_max"] == "0"
    assert probe["pids_max"] == str(caps.SANDBOX_PIDS)
    quota, period = probe["cpu_max"].split()
    assert int(quota) / int(period) == caps.SANDBOX_CPUS
    assert probe["nofile"] == [256, 256]


# --- network -------------------------------------------------------------------------------


def test_http_request_fails() -> None:
    code = (
        "import urllib.request\n\n"
        "def transform(values):\n"
        "    urllib.request.urlopen('http://example.com', timeout=3)\n"
        "    return values\n"
    )
    result = run_transform(code, VALUES)
    assert result.ok is False
    assert result.error is not None and "URLError" in result.error


def test_network_probe_reaches_nothing_and_host_listener_sees_no_connection() -> None:
    # A listener on the host's loopback: Colima's gateway (192.168.5.2) forwards to it, so a
    # container with any network could reach it. It must see no connection.
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.5)
    port = listener.getsockname()[1]
    try:
        code = _probe(
            f"""
import socket
outcomes = []
for host in ["192.168.5.2", "host.docker.internal", "host.lima.internal", "1.1.1.1"]:
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect((host, {port}))
        outcomes.append("connected")
    except OSError as exc:
        outcomes.append(type(exc).__name__)
    finally:
        s.close()
ifaces = sorted(name for _, name in socket.if_nameindex())
result = "|".join(outcomes) + "|ifaces=" + ",".join(ifaces)
"""
        )
        result = run_transform(code, VALUES)
        assert result.ok, result.error
        assert result.values is not None
        probe = result.values[0]
        assert "connected" not in probe
        assert probe.endswith("|ifaces=lo")
        with pytest.raises(TimeoutError):
            listener.accept()
    finally:
        listener.close()


# --- filesystem ----------------------------------------------------------------------------


def test_write_to_in_mount_fails_and_host_files_unchanged(
    isolated: dict[str, dict[str, str]],
) -> None:
    code = (
        "def transform(values):\n"
        "    open('/work/in/transform.py', 'a').write('# pwned')\n"
        "    return values\n"
    )
    result = run_transform(code, VALUES)
    assert result.ok is False
    assert result.error is not None and "Read-only file system" in result.error
    assert isolated["in"] == _in_hashes(code)


def test_write_probe_everywhere_but_out_and_tmp_is_refused(
    isolated: dict[str, dict[str, str]],
) -> None:
    code = _probe(
        """
import os
outcomes = {}
for path in ["/work/in/values.parquet", "/work/in/new.txt", "/etc/pwned", "/etc/passwd",
             "/app/run_transform.py", "/usr/local/lib/pwned.py", "/root/x", "/pwned"]:
    try:
        with open(path, "a") as f:
            f.write("pwned")
        outcomes[path] = "wrote"
    except OSError as exc:
        outcomes[path] = exc.errno
for path in ["/tmp/ok", "/work/out/scratch"]:
    with open(path, "w") as f:
        f.write("fine")
    outcomes[path] = "wrote"
result = __import__("json").dumps(outcomes)
"""
    )
    result = run_transform(code, VALUES)
    assert result.ok, result.error
    assert result.values is not None
    outcomes = json.loads(result.values[0])
    refused = {k: v for k, v in outcomes.items() if not k.startswith(("/tmp", "/work/out"))}
    assert set(refused.values()) <= {30, 13}  # EROFS, EACCES (never "wrote")
    assert outcomes["/tmp/ok"] == "wrote" and outcomes["/work/out/scratch"] == "wrote"
    assert isolated["in"] == _in_hashes(code)


def test_write_to_etc_fails() -> None:
    code = "def transform(values):\n    open('/etc/pwned', 'w').write('x')\n    return values\n"
    result = run_transform(code, VALUES)
    assert result.ok is False
    assert result.error is not None and "Read-only file system" in result.error


def test_symlink_output_to_host_file_is_not_followed() -> None:
    secret_dir = IT_ROOT / ("secret-" + uuid.uuid4().hex)
    secret_dir.mkdir(parents=True)
    secret = secret_dir / "secret.parquet"
    pl.DataFrame({"values": ["SECRET"] * len(VALUES)}).write_parquet(
        secret, compression="uncompressed"
    )
    before = secret.read_bytes()
    try:
        code = (
            "import os\n\n"
            "def transform(values):\n"
            f"    os.symlink({str(secret)!r}, '/work/out/values.parquet')\n"
            "    os._exit(0)\n"
        )
        result = run_transform(code, VALUES)
        assert result.ok is False
        assert result.error is not None and "regular file" in result.error
        assert secret.read_bytes() == before
    finally:
        shutil.rmtree(secret_dir)


def test_output_flood_is_killed_before_filling_disk() -> None:
    code = (
        "def transform(values):\n"
        "    with open('/work/out/junk', 'wb') as f:\n"
        "        while True:\n"
        "            f.write(b'\\0' * 1_048_576)\n"
    )
    result = run_transform(code, VALUES)
    assert result.ok is False
    assert result.error is not None and "output" in result.error and "limit" in result.error
    assert result.duration_ms < caps.SANDBOX_TIMEOUT_S * 1000


# --- environment ---------------------------------------------------------------------------


def test_environment_has_no_host_secrets() -> None:
    # /proc/self/environ is the environment the process was started with (os.environ also has
    # what imports set later, e.g. polars' allocator config).
    code = _probe(
        """
import json, os
start = open("/proc/self/environ", "rb").read().split(b"\\0")
start_env = dict(kv.decode().split("=", 1) for kv in start if kv)
result = json.dumps({"start": start_env, "now": dict(os.environ)})
"""
    )
    result = run_transform(code, VALUES)
    assert result.ok, result.error
    assert result.values is not None
    probe = json.loads(result.values[0])
    env = probe["start"]
    for seen in (env, probe["now"]):
        assert "GROQ_API_KEY" not in seen and "GEMINI_API_KEY" not in seen
    assert FAKE_GROQ not in result.values[0] and FAKE_GEMINI not in result.values[0]
    # Only what the image and Docker set: no variables forwarded from the host.
    assert set(env) <= {
        "PATH", "LANG", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256", "HOME", "HOSTNAME",
        "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED",
    }  # fmt: skip


def test_reading_a_secret_from_env_fails() -> None:
    code = (
        "import os\n\ndef transform(values):\n    os.environ['GROQ_API_KEY']\n    return values\n"
    )
    result = run_transform(code, VALUES)
    assert result.ok is False
    assert result.error is not None and "KeyError" in result.error
    assert FAKE_GROQ not in result.error


# --- resource exhaustion -------------------------------------------------------------------


def test_fork_bomb_hits_pids_limit() -> None:
    code = (
        "import os, time\n\n"
        "def transform(values):\n"
        "    while True:\n"
        "        if os.fork() == 0:\n"
        "            time.sleep(600)\n"
        "            os._exit(0)\n"
    )
    result = run_transform(code, VALUES)
    assert result.ok is False
    assert result.error is not None
    assert "BlockingIOError" in result.error or "Resource temporarily unavailable" in result.error
    # The host can still start processes and run the sandbox afterwards.
    assert run_transform("def transform(values):\n    return values\n", VALUES).ok


def test_memory_blow_up_is_contained() -> None:
    code = "def transform(values):\n    b = bytearray(2_000_000_000)\n    return values\n"
    result = run_transform(code, VALUES)
    assert result.ok is False
    assert result.error is not None
    assert "memory" in result.error or "MemoryError" in result.error


def test_infinite_loop_times_out() -> None:
    code = "def transform(values):\n    while True:\n        pass\n"
    result = run_transform(code, VALUES, timeout_s=3)
    assert result.ok is False
    assert result.error is not None and "timeout" in result.error
    assert 3000 <= result.duration_ms < 10_000
