"""The ``agent`` eval system: :func:`sluice.agent.pipeline.run_pipeline` with real services."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from sluice.agent.events import EventLog
from sluice.agent.pipeline import PipelineDeps, Sandbox, run_pipeline
from sluice.evals.types import CaseInput, SystemOutput
from sluice.llm.client import LLM
from sluice.sandbox.runner import run_transform
from sluice.settings import get_settings


def _default_llm() -> LLM:
    """The LiteLLM-proxy client from :class:`~sluice.settings.Settings` (read at call time)."""
    settings = get_settings()
    return LLM(settings.litellm_url, settings.litellm_master_key.get_secret_value())


class AgentSystem:
    """Eval :class:`~sluice.evals.types.System` running the agent pipeline on one file per call.

    Each :meth:`run` is one ``asyncio.run``; the :class:`~sluice.llm.client.LLM` is created and
    closed inside it (its HTTP pool is bound to that loop). Settings are read on the first model
    client creation, not at construction, so registering the system needs no environment.

    Args:
        llm_factory: Builds the model client, called inside the run's event loop.
        sandbox: Runs one transform (default: the Docker sandbox, ``run_transform``).
        clock: Monotonic seconds, for latency and the wall-clock cap.
    """

    name = "agent"

    def __init__(
        self,
        llm_factory: Callable[[], LLM] = _default_llm,
        sandbox: Sandbox = run_transform,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._llm_factory = llm_factory
        self._sandbox = sandbox
        self._clock = clock

    def run(self, case: CaseInput) -> SystemOutput:
        """Run the pipeline over ``case.path`` and return its output (events in ``meta``)."""
        return asyncio.run(self._run(case))

    async def _run(self, case: CaseInput) -> SystemOutput:
        async with self._llm_factory() as llm:
            deps = PipelineDeps(llm=llm, sandbox=self._sandbox, clock=self._clock)
            return await run_pipeline(case, deps, EventLog())
