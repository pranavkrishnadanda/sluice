"""Statistics: bootstrap CIs, pass@k / pass^k and McNemar's exact test."""

import time

import pytest
from scipy.stats import binomtest

from sluice.evals.stats import bootstrap_ci, clopper_pearson, mcnemar_p, pass_at_k, pass_hat_k

# --- brief tests (verbatim) ---------------------------------------------------------------


def test_bootstrap_constant():
    assert bootstrap_ci([0.7] * 20) == (0.7, 0.7, 0.7)


def test_bootstrap_brackets():
    m, lo, hi = bootstrap_ci([0.0, 1.0] * 50)
    assert m == 0.5 and lo < 0.5 < hi


def test_bootstrap_none():
    assert bootstrap_ci([None, None]) is None


def test_pass_at_k():
    assert pass_at_k(4, 2, 2) == pytest.approx(5 / 6)


def test_pass_hat_k():
    assert pass_hat_k(4, 2, 2) == pytest.approx(1 / 6)


def test_pass_hat_k_edges():
    assert pass_hat_k(4, 4, 4) == 1.0 and pass_hat_k(4, 3, 4) == 0.0


def test_mcnemar_extreme():
    assert mcnemar_p([True] * 10, [False] * 10) == pytest.approx(2 * 0.5**10)


def test_mcnemar_identical():
    assert mcnemar_p([True, False], [True, False]) == 1.0


# --- bootstrap_ci ---------------------------------------------------------------------------


def test_bootstrap_empty_returns_none():
    assert bootstrap_ci([]) is None


def test_bootstrap_drops_none():
    assert bootstrap_ci([None, 0.7, None, 0.7]) == (0.7, 0.7, 0.7)


def test_bootstrap_single_value():
    assert bootstrap_ci([0.25]) == (0.25, 0.25, 0.25)


def test_bootstrap_returns_python_floats():
    result = bootstrap_ci([0.1, 0.4, 0.9, 0.3])
    assert result is not None
    assert all(type(x) is float for x in result)


def test_bootstrap_deterministic_for_seed():
    values = [i / 37 for i in range(40)]
    assert bootstrap_ci(values, seed=3) == bootstrap_ci(values, seed=3)


def test_bootstrap_seed_changes_interval():
    values = [i / 37 for i in range(40)]
    assert bootstrap_ci(values, seed=1)[1:] != bootstrap_ci(values, seed=2)[1:]


def test_bootstrap_mean_is_sample_mean():
    m, _, _ = bootstrap_ci([0.0, 0.0, 0.0, 1.0])
    assert m == 0.25


def test_bootstrap_wider_alpha_gives_narrower_interval():
    values = [0.0, 1.0] * 50
    _, lo95, hi95 = bootstrap_ci(values, alpha=0.05)
    _, lo50, hi50 = bootstrap_ci(values, alpha=0.5)
    assert lo95 <= lo50 < hi50 <= hi95


def test_bootstrap_interval_within_data_range():
    values = [0.2, 0.9, 0.5, 0.4, 0.8, 0.3]
    _, lo, hi = bootstrap_ci(values)
    assert min(values) <= lo <= hi <= max(values)


def test_bootstrap_ci_roughly_matches_normal_theory():
    # Bernoulli(0.5), n = 100: standard error 0.05, so the 95% CI is about 0.5 +/- 0.098.
    _, lo, hi = bootstrap_ci([0.0, 1.0] * 50)
    assert 0.38 < lo < 0.42 and 0.58 < hi < 0.62


@pytest.mark.parametrize("alpha", [0.0, 1.0, -0.1, 1.5])
def test_bootstrap_rejects_bad_alpha(alpha):
    with pytest.raises(ValueError, match="alpha"):
        bootstrap_ci([0.1, 0.2], alpha=alpha)


@pytest.mark.parametrize("n_resamples", [0, -5])
def test_bootstrap_rejects_bad_n_resamples(n_resamples):
    with pytest.raises(ValueError, match="n_resamples"):
        bootstrap_ci([0.1, 0.2], n_resamples=n_resamples)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_bootstrap_rejects_non_finite(bad):
    with pytest.raises(ValueError, match="finite"):
        bootstrap_ci([0.1, bad])


def test_bootstrap_300_values_10k_resamples_is_fast():
    values = [(i * 7919 % 300) / 300 for i in range(300)]
    bootstrap_ci(values)  # warm-up (imports, allocator)
    start = time.perf_counter()
    bootstrap_ci(values)
    assert time.perf_counter() - start < 1.0


# --- pass_at_k / pass_hat_k ----------------------------------------------------------------


def test_pass_at_k_edges():
    assert pass_at_k(4, 0, 2) == 0.0
    assert pass_at_k(4, 3, 2) == 1.0  # fewer than k failures: any k draws include a success
    assert pass_at_k(5, 2, 1) == pytest.approx(0.4)


def test_pass_hat_k_k1_is_success_rate():
    assert pass_hat_k(5, 2, 1) == pytest.approx(0.4)


def test_pass_hat_k_zero_successes():
    assert pass_hat_k(3, 0, 3) == 0.0


def test_pass_hat_never_exceeds_pass_at():
    for n in range(1, 8):
        for c in range(n + 1):
            for k in range(1, n + 1):
                assert pass_hat_k(n, c, k) <= pass_at_k(n, c, k)


def test_pass_returns_python_float():
    assert type(pass_at_k(4, 2, 2)) is float and type(pass_hat_k(4, 2, 2)) is float


@pytest.mark.parametrize("fn", [pass_at_k, pass_hat_k])
@pytest.mark.parametrize(("n", "c", "k"), [(4, 5, 2), (4, -1, 2), (4, 2, 0), (4, 2, 5), (0, 0, 1)])
def test_pass_rejects_invalid(fn, n, c, k):
    with pytest.raises(ValueError):
        fn(n, c, k)


# --- mcnemar_p -----------------------------------------------------------------------------


def test_mcnemar_symmetric():
    a = [True, True, True, False, False, True]
    b = [False, False, True, True, False, True]
    assert mcnemar_p(a, b) == mcnemar_p(b, a)


def test_mcnemar_balanced_discordance_is_one():
    assert mcnemar_p([True, False], [False, True]) == 1.0


def test_mcnemar_empty_is_one():
    assert mcnemar_p([], []) == 1.0


def test_mcnemar_known_value():
    # 1 vs 4 discordant: two-sided exact p = 2 * (C(5,0) + C(5,1)) / 2**5 = 0.375
    a = [True] + [False] * 4 + [True] * 3
    b = [False] + [True] * 4 + [True] * 3
    assert mcnemar_p(a, b) == pytest.approx(0.375)


def test_mcnemar_returns_python_float():
    assert type(mcnemar_p([True] * 3, [False] * 3)) is float


def test_mcnemar_rejects_length_mismatch():
    with pytest.raises(ValueError, match="length"):
        mcnemar_p([True], [True, False])


# --- clopper_pearson (boundary intervals in RESULTS.md) ------------------------------------


def test_clopper_pearson_zero_successes():
    lo, hi = clopper_pearson(0, 67)
    assert lo == 0.0
    assert hi == pytest.approx(1 - 0.025 ** (1 / 67))  # closed form at k = 0
    assert round(hi * 100, 1) == 5.4


def test_clopper_pearson_all_successes():
    lo, hi = clopper_pearson(90, 90)
    assert hi == 1.0
    assert lo == pytest.approx(0.025 ** (1 / 90))  # closed form at k = n
    assert round(lo * 100, 1) == 96.0


def test_clopper_pearson_interior_brackets_the_rate():
    lo, hi = clopper_pearson(30, 100)
    assert lo < 0.3 < hi
    ref = binomtest(30, 100).proportion_ci(confidence_level=0.95, method="exact")
    assert (lo, hi) == pytest.approx((ref.low, ref.high))


def test_clopper_pearson_returns_python_floats():
    assert all(type(v) is float for v in clopper_pearson(3, 10))


@pytest.mark.parametrize(("k", "n"), [(0, 0), (-1, 5), (6, 5)])
def test_clopper_pearson_rejects_invalid(k, n):
    with pytest.raises(ValueError):
        clopper_pearson(k, n)
