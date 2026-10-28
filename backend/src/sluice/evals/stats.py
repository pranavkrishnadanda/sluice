"""Statistics for published eval numbers: bootstrap CIs, pass@k / pass^k and McNemar's test.

* ``bootstrap_ci`` -- percentile bootstrap of the mean. Undefined values (``None``) are dropped
  first; the point estimate is the exact sample mean (``math.fsum``), the interval is the
  ``alpha / 2`` and ``1 - alpha / 2`` quantiles of ``n_resamples`` resampled means. Resampling
  is vectorised (an index matrix per chunk) and deterministic given ``seed``.
* ``pass_at_k`` -- unbiased estimator of "at least one of ``k`` trials succeeds", from ``n``
  trials with ``c`` successes: ``1 - C(n - c, k) / C(n, k)`` (Chen et al. 2021, Codex).
* ``pass_hat_k`` -- tau-bench's pass^k, "all ``k`` trials succeed": ``C(c, k) / C(n, k)``
  (Yao et al. 2024). It is the probability that ``k`` trials drawn without replacement from the
  ``n`` observed are all successes, an unbiased estimator of ``p ** k``. ``pass_hat_k(n, c, n)``
  is 1.0 iff every trial succeeded. Both use exact integer combinatorics.
* ``mcnemar_p`` -- exact two-sided McNemar test on paired booleans: a binomial test with
  ``p = 0.5`` on the discordant pairs (``a`` right / ``b`` wrong vs. ``a`` wrong / ``b``
  right). 1.0 when there are no discordant pairs.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from fractions import Fraction

import numpy as np
from scipy.stats import binomtest

# Cap on elements of one resample index matrix (int64), bounding peak memory at ~32 MiB.
_MAX_CHUNK_ELEMENTS = 1 << 22


def bootstrap_ci(
    values: Sequence[float | None],
    n_resamples: int = 10_000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float, float] | None:
    """Return ``(mean, lo, hi)``: the mean and its ``1 - alpha`` percentile-bootstrap interval.

    ``None`` entries are dropped; returns ``None`` if nothing remains. Constant input returns
    ``(v, v, v)`` exactly. Time ``O(n_resamples * n)``; memory bounded by
    ``_MAX_CHUNK_ELEMENTS``.

    Raises:
        ValueError: if ``n_resamples < 1``, ``alpha`` is not in ``(0, 1)``, or a value is not
            finite.
    """
    if n_resamples < 1:
        raise ValueError(f"n_resamples must be >= 1, got {n_resamples}")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    kept = [float(v) for v in values if v is not None]
    if not kept:
        return None
    if not all(math.isfinite(v) for v in kept):
        raise ValueError("bootstrap_ci values must be finite")

    mean = math.fsum(kept) / len(kept)
    lo_v, hi_v = min(kept), max(kept)
    if lo_v == hi_v:  # every resample mean is the constant; skip float noise entirely
        return (lo_v, lo_v, lo_v)

    data = np.asarray(kept, dtype=np.float64)
    n = data.size
    rng = np.random.default_rng(seed)
    means = np.empty(n_resamples, dtype=np.float64)
    rows_per_chunk = max(1, _MAX_CHUNK_ELEMENTS // n)
    for start in range(0, n_resamples, rows_per_chunk):
        stop = min(start + rows_per_chunk, n_resamples)
        idx = rng.integers(0, n, size=(stop - start, n))
        means[start:stop] = data[idx].mean(axis=1)

    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    # A resampled mean cannot leave the data range; clamp away last-ulp summation error.
    return (mean, float(min(max(lo, lo_v), hi_v)), float(min(max(hi, lo_v), hi_v)))


def _check_nck(n: int, c: int, k: int) -> None:
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    if not 0 <= c <= n:
        raise ValueError(f"c must satisfy 0 <= c <= n ({n}), got {c}")
    if not 1 <= k <= n:
        raise ValueError(f"k must satisfy 1 <= k <= n ({n}), got {k}")


def pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased pass@k from ``n`` trials with ``c`` successes: ``1 - C(n-c, k) / C(n, k)``.

    Raises:
        ValueError: unless ``n >= 1``, ``0 <= c <= n`` and ``1 <= k <= n``.
    """
    _check_nck(n, c, k)
    return float(1 - Fraction(math.comb(n - c, k), math.comb(n, k)))


def pass_hat_k(n: int, c: int, k: int) -> float:
    """tau-bench pass^k from ``n`` trials with ``c`` successes: ``C(c, k) / C(n, k)``.

    The probability that ``k`` of the ``n`` trials, drawn without replacement, all succeed.

    Raises:
        ValueError: unless ``n >= 1``, ``0 <= c <= n`` and ``1 <= k <= n``.
    """
    _check_nck(n, c, k)
    return float(Fraction(math.comb(c, k), math.comb(n, k)))


def mcnemar_p(a: Sequence[bool], b: Sequence[bool]) -> float:
    """Exact two-sided McNemar p-value for paired outcomes ``a[i]`` vs. ``b[i]``.

    Binomial test (``p = 0.5``) on the discordant pairs; 1.0 if there are none.

    Raises:
        ValueError: if ``a`` and ``b`` differ in length.
    """
    if len(a) != len(b):
        raise ValueError(f"mcnemar_p needs equal-length inputs, got {len(a)} and {len(b)}")
    a_only = sum(1 for x, y in zip(a, b, strict=True) if x and not y)
    b_only = sum(1 for x, y in zip(a, b, strict=True) if y and not x)
    discordant = a_only + b_only
    if discordant == 0:
        return 1.0
    return float(binomtest(a_only, discordant, p=0.5).pvalue)
