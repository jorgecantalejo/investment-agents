"""Estudio de factores macro del oro: regímenes, solidez, retraso de publicación y degradación sin datos."""

import numpy as np
import pandas as pd

from src.core.errors import NetworkError
from src.market.service import OhlcResult
from src.models import FactorRegime
from src.quant import gold_factors as gf
from src.quant.gold_factors import FactorSpec, build_factor_study, regime_stats, verdict

PCT = FactorSpec("f", "Factor", "yahoo", "F", "level", "pct", 0.5, "", "Factor al alza → oro al alza.", 1)
RATE = FactorSpec("r", "Tipo", "fred", "R", "level", "diff", 0.05, "%", "Tipos al alza → oro a la baja.", -1)


def _monthly_pair(months: int = 240, seed: int = 1) -> tuple[pd.Series, pd.Series]:
    """Factor con tramos alternos de subida y bajada; el oro sube fuerte tras las subidas del factor y baja tras las bajadas."""
    idx = pd.date_range("2005-01-31", periods=months, freq="ME")
    rng = np.random.default_rng(seed)
    steps = np.where((np.arange(months) // 6) % 2 == 0, 2.0, -2.0)
    factor = pd.Series(100 + np.cumsum(steps), index=idx)
    trend = factor.diff(3).fillna(0).shift(1).fillna(0)  # el oro reacciona al mes siguiente
    gold = pd.Series(1000 * np.cumprod(1 + np.sign(trend) * 0.03 + rng.normal(0, 0.005, months)), index=idx)
    return gold, factor


def test_regime_stats_detects_relationship_and_verdict_is_favorable():
    gold, factor = _monthly_pair()
    rising, falling = regime_stats(gold, factor, PCT)
    assert rising.months > 24 and falling.months > 24
    assert rising.avg_return_3m_pct > 0 > falling.avg_return_3m_pct
    edge, t, observed, signal = verdict("up", rising, falling)
    assert signal == "favorable" and observed == 1 and t > gf.MIN_T and edge > gf.EDGE_PP
    assert verdict("down", rising, falling)[3] == "unfavorable"
    assert verdict("flat", rising, falling)[3] == "neutral"


def test_verdict_requires_samples_and_solidity():
    small = FactorRegime(months=10, avg_return_3m_pct=5, std_3m_pct=1)
    big = FactorRegime(months=100, avg_return_3m_pct=0, std_3m_pct=1)
    assert verdict("up", small, big)[3] == "insufficient_data"
    noisy_up = FactorRegime(months=100, avg_return_3m_pct=2.0, std_3m_pct=40)
    noisy_down = FactorRegime(months=100, avg_return_3m_pct=0.5, std_3m_pct=40)
    edge, t, observed, signal = verdict("up", noisy_up, noisy_down)
    assert edge == 1.5 and abs(t) < gf.MIN_T and observed == 0 and signal == "neutral"


def test_monthly_series_shifted_one_month_for_publication(monkeypatch):
    cpi = FactorSpec("cpi", "IPC", "fred", "CPI", "yoy", "diff", 0.1, "%", "x", 1)
    idx = pd.date_range("2020-01-01", periods=30, freq="MS")
    monkeypatch.setattr(gf.fred, "fetch_series", lambda s, start: pd.Series(np.linspace(100, 130, 30), index=idx))

    class NoCache:
        def get(self, key, allow_stale=False):
            return None

        def set(self, key, value):
            pass

    series = gf.factor_series(cpi, None, NoCache())  # type: ignore[arg-type]
    # El IPC de enero de 2021 (primer interanual) se considera conocido a final de febrero de 2021
    assert series.index[0] == pd.Timestamp("2021-02-28")


class FakeService:
    def __init__(self, frames: dict[str, pd.Series]):
        self.frames = frames

    def get_ohlc(self, ticker, period=None):
        if ticker not in self.frames:
            raise NetworkError(f"sin datos de {ticker}")
        close = self.frames[ticker]
        return OhlcResult(pd.DataFrame({"open": close, "high": close, "low": close, "close": close}))


def _daily(values: np.ndarray, start: str = "2004-01-01") -> pd.Series:
    return pd.Series(values, index=pd.bdate_range(start, periods=len(values)))


def test_build_study_degrades_per_factor_and_never_raises():
    n = 3000
    rng = np.random.default_rng(3)
    frames = {"GC=F": _daily(1000 * np.cumprod(1 + rng.normal(0.0003, 0.01, n))), "EURUSD=X": _daily(np.full(n, 1.1)),
              "F": _daily(100 * np.cumprod(1 + rng.normal(0, 0.01, n)))}
    study = build_factor_study(FakeService(frames), None, specs=[PCT, RATE])  # type: ignore[arg-type]
    assert study.error is None and study.months > 100 and study.base_avg_3m_pct is not None
    ok, missing = study.factors
    assert ok.error is None and ok.value is not None and ok.direction in ("up", "down", "flat") and ok.corr_52w is not None
    assert missing.error and missing.signal == "insufficient_data"  # FRED sin caché macro
    assert study.favorable + study.unfavorable + study.neutral <= 1


def test_build_study_without_gold_price():
    study = build_factor_study(FakeService({}), None, specs=[PCT])  # type: ignore[arg-type]
    assert study.error and not study.factors
