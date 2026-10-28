"""Requirements for the sandbox image, taken from ``uv.lock`` so the image runs the same polars.

``make sandbox-image`` runs ``python -m sluice.sandbox.image uv.lock`` and feeds the output to
``pip install --require-hashes --no-deps --only-binary=:all:`` in ``backend/sandbox/Dockerfile``.
Only ``polars`` and its locked dependency closure (``polars-runtime-32``) are emitted, each pinned
to the locked version with every locked wheel hash, so pip cannot pick anything else.
"""

import sys
import tomllib
from pathlib import Path

ROOT_PACKAGE = "polars"


def locked_requirements(lock_path: Path, package: str = ROOT_PACKAGE) -> str:
    """Return a hash-pinned requirements file for ``package`` and its locked dependencies.

    Raises:
        KeyError: ``package`` (or one of its dependencies) is not in the lockfile.
        ValueError: a locked package has no wheel hashes (an sdist-only build is not allowed).
    """
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    packages = {p["name"]: p for p in lock["package"]}
    order: list[str] = []
    pending = [package]
    while pending:
        name = pending.pop(0)
        if name in order:
            continue
        order.append(name)
        pending.extend(dep["name"] for dep in packages[name].get("dependencies", []))

    lines = [f"# Generated from {lock_path.name} by sluice.sandbox.image; do not edit."]
    for name in order:
        entry = packages[name]
        hashes = [w["hash"] for w in entry.get("wheels", [])]
        if not hashes:
            raise ValueError(f"{name} has no locked wheels")
        pins = " ".join(f"--hash={h}" for h in hashes)
        lines.append(f"{name}=={entry['version']} {pins}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.stdout.write(locked_requirements(Path(sys.argv[1] if len(sys.argv) > 1 else "uv.lock")))
