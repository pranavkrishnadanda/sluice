"""Hard limits for the agent pipeline. Every cap lives here so it is easy to find and audit."""

from typing import Final

MAX_ATTEMPTS: Final = 3
"""Attempts per plan node (first try plus retries) before the node is given up on."""

MAX_TOKENS_PER_FILE: Final = 60_000
"""Model tokens one file may spend; once reached, no new attempts start."""

MAX_WALL_S_PER_FILE: Final = 300
"""Wall-clock seconds one file may take; once reached, no new attempts start.

Raised from 180 s with the strong deployments' 60 s per-attempt timeout: it must exceed the
client's ``REQUEST_TIMEOUT_S`` (180 s), see ``tests/llm/test_litellm_config.py``."""

SANDBOX_TIMEOUT_S: Final = 20
"""Seconds one generated transform may run inside the sandbox."""

SANDBOX_MEM_MB: Final = 512
"""Memory limit of the sandbox container, in MiB."""

SANDBOX_CPUS: Final = 1.0
"""CPUs the sandbox container may use (Docker ``nano_cpus``)."""

SANDBOX_PIDS: Final = 64
"""Process limit of the sandbox container (fork-bomb guard)."""

MAX_ERROR_CHARS: Final = 2_000
"""Longest ``attempt_failed.error`` sent to clients (``MAX_ATTEMPT_ERROR_CHARS`` in the API)."""

SANDBOX_OUTPUT_MAX_BYTES: Final = 50 * 1024 * 1024
"""Largest sandbox output the host accepts: on disk while the container runs, and decoded."""
