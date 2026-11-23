"""Tests de los cálculos cuantitativos puros."""

import math

import numpy as np
import pandas as pd
import pytest

from src.quant import metrics


def test_period_return_uses_price_on_or_before_target_date():
    idx = pd.to_datetime(["2025-01-15", "2025-02-14", "2025-02-17", "2025-03-17"])
    close = pd.Series([100.0, 110.0, 120.0, 132.0], index=idx)
    # Hace 1 mes desde 2025-03-17 = 2025-02-17 -> 120
    assert metrics.period_return(close, 1) == pytest.approx(10.0)
    # Hace 2 meses = 2025-01-17 -> último cierre previo 2025-01-15 (100)
    assert metrics.period_return(close, 2) == pytest.approx(32.0)


def test_period_return_insufficient_history_returns_none(linear_series):
    short = linear_series.iloc[:150]  # ~7 meses
    assert metrics.period_return(short, 6) is not None
    assert metrics.period_return(short, 12) is None
    assert metrics.period_return(pd.Series(dtype=float), 1) is None


def test_returns_by_period_keys(linear_series):
    result = metrics.returns_by_period(linear_series)
    assert set(result) == {"1m", "3m", "6m", "12m"}
    assert all(v is not None and v > 0 for v in result.values())  # 300 días hábiles ≈ 14 meses


def test_simple_moving_average(linear_series):
    # Últimas 50 observaciones: 350..399 -> media 374.5
    assert metrics.simple_moving_average(linear_series, 50) == pytest.approx(374.5)
    assert metrics.simple_moving_average(linear_series, 200) == pytest.approx(299.5)
    assert metrics.simple_moving_average(linear_series.iloc[:10], 50) is None


def test_drawdown_from_high():
    idx = pd.bdate_range("2026-01-01", periods=5)
    close = pd.Series([100.0, 120.0, 90.0, 96.0, 108.0], index=idx)
    dd, high, high_date = metrics.drawdown_from_high(close, 365)
    assert dd == pytest.approx(-10.0)
    assert high == 120.0
    assert high_date == idx[1].strftime("%Y-%m-%d")


def test_drawdown_at_new_high_is_zero(linear_series):
    dd, _, _ = metrics.drawdown_from_high(linear_series, 365)
    assert dd == 0.0


def test_max_drawdown():
    idx = pd.bdate_range("2026-01-01", periods=5)
    close = pd.Series([100.0, 120.0, 90.0, 96.0, 130.0], index=idx)
    assert metrics.max_drawdown(close, 365) == pytest.approx(-25.0)


def test_annualized_volatility_constant_growth_is_near_zero():
    idx = pd.bdate_range("2025-01-01", periods=260)
    close = pd.Series(100 * 1.001 ** np.arange(260), index=idx)
    assert metrics.annualized_volatility(close) == pytest.approx(0.0, abs=1e-6)


def test_annualized_volatility_matches_formula():
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2025-01-01", periods=253)
    rets = rng.normal(0, 0.01, 252)
    close = pd.Series(100 * np.cumprod(np.r_[1, 1 + rets]), index=idx)
    vol = metrics.annualized_volatility(close)
    assert vol == pytest.approx(np.std(rets, ddof=1) * math.sqrt(252) * 100, rel=0.05)


def test_relative_performance_handles_missing():
    rel = metrics.relative_performance({"1m": 5.0, "12m": None}, {"1m": 2.0, "12m": 3.0})
    assert rel == {"1m": 3.0, "12m": None}


@pytest.mark.parametrize(
    "price,sma50,sma200,ret6,expected",
    [(110, 105, 100, 5, "up"), (90, 95, 100, -5, "down"), (101, 99, 100, 2, "sideways"), (100, None, 100, 1, "insufficient_data")],
)
def test_classify_trend(price, sma50, sma200, ret6, expected):
    assert metrics.classify_trend(price, sma50, sma200, ret6) == expected


def test_weekly_closes_ends_on_last_price(linear_series):
    weekly = metrics.weekly_closes(linear_series, 365)
    assert 50 <= len(weekly) <= 54
    assert weekly[-1] == linear_series.iloc[-1]
    assert weekly == sorted(weekly)  # serie creciente
