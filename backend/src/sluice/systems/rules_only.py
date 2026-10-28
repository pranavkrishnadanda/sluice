"""The rules-only baseline: read, profile, apply deterministic rules. No model calls."""

import time

import polars as pl

from sluice.evals.types import CaseInput, SystemOutput
from sluice.io import read_input
from sluice.rules.fixers import UnkeyableInputError, apply_rules
from sluice.rules.profile import profile


class RulesOnlySystem:
    """Baseline :class:`~sluice.evals.types.System`; it never fabricates a value.

    A file whose primary key cannot be located is quarantined whole (empty output, no flags):
    without a key no row can be addressed, deduplicated or flagged.
    """

    name = "rules"

    def run(self, case: CaseInput) -> SystemOutput:
        """Run the rules over ``case.path``."""
        start = time.perf_counter()
        df = read_input(case.path)
        prof = profile(df, case.contract)
        try:
            result = apply_rules(df, case.contract, prof, case.reference)
        except UnkeyableInputError as exc:
            empty = pl.DataFrame(schema=case.contract.polars_schema())
            return SystemOutput(
                df=empty,
                flags=set(),
                quarantined=set(),
                file_quarantined=True,
                cost_usd=0.0,
                latency_s=time.perf_counter() - start,
                meta={"actions": [f"file quarantined: {exc}"]},
            )
        return SystemOutput(
            df=result.df,
            flags=result.flags,
            quarantined=result.quarantined,
            file_quarantined=False,
            cost_usd=0.0,
            latency_s=time.perf_counter() - start,
            meta={"actions": result.actions},
        )
