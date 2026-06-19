import pytest
from trader.learning.ic_metrics import compute_ic, compute_icir, ic_weight_nudge


def test_compute_ic_perfect_positive_correlation():
    strengths = [0.1, 0.3, 0.5, 0.7, 0.9]
    returns =   [0.1, 0.3, 0.5, 0.7, 0.9]
    ic = compute_ic(strengths, returns)
    assert ic is not None
    assert abs(ic - 1.0) < 1e-9


def test_compute_ic_perfect_negative_correlation():
    strengths = [0.9, 0.7, 0.5, 0.3, 0.1]
    returns =   [0.1, 0.3, 0.5, 0.7, 0.9]
    ic = compute_ic(strengths, returns)
    assert ic is not None
    assert abs(ic - (-1.0)) < 1e-9


def test_compute_ic_returns_none_below_min_pairs():
    assert compute_ic([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0]) is None


def test_compute_ic_returns_none_mismatched_lengths():
    assert compute_ic([1.0, 2.0, 3.0, 4.0, 5.0], [1.0, 2.0]) is None


def test_compute_icir_returns_none_below_min():
    assert compute_icir([0.1, 0.2]) is None


def test_compute_icir_returns_none_on_zero_std():
    assert compute_icir([0.5, 0.5, 0.5]) is None


def test_compute_icir_positive_series():
    series = [0.1, 0.15, 0.12, 0.18, 0.14]
    icir = compute_icir(series)
    assert icir is not None
    assert icir > 0


def test_ic_weight_nudge_returns_zero_for_none():
    assert ic_weight_nudge(None) == 0.0


def test_ic_weight_nudge_positive_icir_positive_nudge():
    nudge = ic_weight_nudge(2.0, scale=0.05)
    assert nudge > 0
    assert nudge <= 0.05


def test_ic_weight_nudge_negative_icir_negative_nudge():
    nudge = ic_weight_nudge(-2.0, scale=0.05)
    assert nudge < 0
    assert nudge >= -0.05


def test_ic_weight_nudge_clamped_at_scale():
    assert ic_weight_nudge(100.0, scale=0.05) == pytest.approx(0.05)
    assert ic_weight_nudge(-100.0, scale=0.05) == pytest.approx(-0.05)


def test_compute_ic_returns_none_on_zero_variance():
    # All strengths identical → np.corrcoef returns nan → must return None
    ic = compute_ic([0.5, 0.5, 0.5, 0.5, 0.5], [0.1, 0.2, 0.3, 0.4, 0.5])
    assert ic is None
