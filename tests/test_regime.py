"""Estrategia de régimen semanal del S&P 500 (SMA40 con banda): indicadores de contexto, regla, choque de un día y resultado."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from src.config.models import RegimeStrategyConfig
from src.core.errors import InvalidTickerError
from src.market.service import OhlcResult
from src.quant.regime import Readings, RegimeParams, chaikin_money_flow, daily_indicators, decide, replay, weekly_frame
from src.quant.regime_snapshot import build_regime_strategy, week_is_complete

P = RegimeParams()
SMALL = dict(sma_fast=2, sma_slow=4, dd_window=5, cmf_window=3, obv_weeks=2)
AFTER_CLOSE = datetime(2030, 1, 1, 20, 0, tzinfo=ZoneInfo("America/New_York"))


def ohlcv(close: list[float], volume: list[float] | None = None, start: str = "2024-01-01") -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=len(close))
    c = pd.Series(close, index=idx, dtype=float)
    v = pd.Series(volume if volume is not None else [1000.0] * len(close), index=idx, dtype=float)
    return pd.DataFrame({"open": c.shift(1).fillna(c.iloc[0]), "high": c * 1.005, "low": c * 0.995, "close": c, "volume": v})


def readings(**kw) -> Readings:
    base = dict(close=110, sma_fast=105, sma_slow=100, dd25=2, cmf=0.1, obv_div=False, vix=15.0,
                prev_cmf_negative=False, prev_vix_high=False)
    return Readings(**{**base, **kw})


def test_distribution_day_needs_drop_and_more_volume():
    df = ohlcv([100, 99.7, 99.5, 99.4], volume=[1000, 1200, 900, 1300])
    d = daily_indicators(df, None, P)
    # −0,3% con más volumen: sí · −0,2% con menos volumen: no · −0,1% con más volumen: no
    assert d["dd"].tolist() == [False, True, False, False]


def test_cmf_sign_follows_close_location():
    df = ohlcv([100.0] * 10)
    df["high"], df["low"] = 101.0, 99.0
    df["close"] = 100.9  # cierres cerca del máximo
    assert chaikin_money_flow(df, 5).iloc[-1] > 0.8
    df["close"] = 99.1
    assert chaikin_money_flow(df, 5).iloc[-1] < -0.8


def test_decide_band_exit():
    assert decide(100, readings(), P) == (100, [])
    # Perforar la SMA40 dentro de la banda (−1,5%) no da orden, aunque el flujo sea malo
    assert decide(100, readings(close=99, sma_fast=95, dd25=12, cmf=-0.3, obv_div=True, vix=40.0), P)[0] == 100
    new, reasons = decide(100, readings(close=98.4), P)
    assert new == 0 and "banda de salida" in reasons[0]


def test_decide_band_entry():
    assert decide(0, readings(close=90), P) == (0, [])
    # Sobre la media pero dentro de la banda de entrada (+1%): se sigue fuera
    assert decide(0, readings(close=100.9), P)[0] == 0
    new, reasons = decide(0, readings(close=101.1, sma_fast=95), P)
    assert new == 100 and "banda de entrada" in reasons[0]


def test_band_zero_is_plain_sma():
    p = RegimeParams(exit_band_pct=0, enter_band_pct=0)
    assert decide(100, readings(close=99.9), p)[0] == 0
    assert decide(0, readings(close=100.1), p)[0] == 100


def test_flow_warnings_are_context_only():
    r = readings(dd25=9, cmf=-0.2, prev_cmf_negative=True, obv_div=True, vix=30.0, prev_vix_high=True)
    assert len(r.flow_warnings(P)) == 4
    assert decide(100, r, P)[0] == 100


def test_replay_whipsaw_inside_band_makes_no_switch():
    p = RegimeParams(**SMALL)
    close = [100.0] * 25 + [99.5, 100.4, 99.6, 100.3] * 3  # viernes alternos ±0,5% alrededor de la media
    weekly_close = [c for c in close for _ in range(5)]
    d = daily_indicators(ohlcv(weekly_close), None, p)
    rep = replay(d, weekly_frame(d, p), p)
    assert rep.switches == [] and rep.exposure == 0


def test_single_friday_crash_is_evaluated_with_thursday_close():
    p = RegimeParams(**SMALL)
    close = list(np.linspace(100, 130, 60))
    close[-1] = close[-2] * 0.91  # viernes −9%
    df = ohlcv(close)
    assert df.index[-1].weekday() == 4
    w = weekly_frame(daily_indicators(df, None, p), p)
    assert bool(w["shock"].iloc[-1])
    assert w["close"].iloc[-1] == pytest.approx(close[-2])


def test_one_day_crash_never_exits_to_zero():
    p = RegimeParams(**SMALL)
    close = list(np.linspace(100, 140, 80)) + [140 * 0.90, 140 * 0.97, 140 * 0.99, 140, 141]  # lunes −10% y rebote
    vol = [1000.0] * 80 + [5000.0, 3000.0, 2000.0, 1500.0, 1200.0]
    df = ohlcv(close, vol, start="2024-01-01")
    d = daily_indicators(df, None, p)
    rep = replay(d, weekly_frame(d, p), p)
    assert all(s.to_pct != 0 for s in rep.switches)


def test_week_is_complete():
    assert week_is_complete(date(2026, 11, 20), date(2026, 11, 20))  # viernes
    assert not week_is_complete(date(2026, 11, 23), date(2026, 11, 23))  # lunes
    assert week_is_complete(date(2026, 4, 2), date(2026, 4, 4))  # Viernes Santo festivo: el sábado la semana ya cerró


class FakeService:
    class prices:
        name = "fake"

    def __init__(self, frame=None, error=None):
        self.frame, self.error = frame, error

    def get_ohlcv(self, ticker, period=None):
        if self.error:
            raise self.error
        return OhlcResult(self.frame)

    def get_ohlc(self, ticker, period=None):
        raise InvalidTickerError("sin VIX")


def regime_cfg(**kw) -> RegimeStrategyConfig:
    return RegimeStrategyConfig(name="S&P 500", asset_id="sp500", **{**SMALL, "sma_fast": 2, "sma_slow": 10, "cmf_window": 5, **kw})


def test_build_regime_strategy_uptrend():
    rng = np.random.default_rng(1)
    close = list(100 * np.cumprod(1 + 0.002 + rng.normal(0, 0.004, 400)))
    res = build_regime_strategy("sp500_regime", regime_cfg(), FakeService(ohlcv(close, list(rng.uniform(900, 1100, 400)))), now=AFTER_CLOSE)
    assert res.error is None
    assert res.exposure == 100 and res.trend == "alcista"
    assert {c.id for c in res.checks} == {"trend", "cross", "dd", "cmf", "obv", "vix"}
    assert res.exit_line < res.sma_slow < res.enter_line and len(res.chart.exit_line) == len(res.chart.dates)
    assert next(c for c in res.checks if c.id == "vix").status == "na"  # sin VIX la regla sigue funcionando
    assert set(res.perf) == {"strategy", "sma_only", "buy_hold"}
    assert res.explanation and res.chart.dates


def test_build_regime_strategy_errors():
    res = build_regime_strategy("x", regime_cfg(), FakeService(error=InvalidTickerError("caído")), now=AFTER_CLOSE)
    assert res.error == "caído" and res.exposure is None
    short = build_regime_strategy("x", regime_cfg(), FakeService(ohlcv([100.0] * 20)), now=AFTER_CLOSE)
    assert "insuficiente" in short.error


def test_config_validation():
    with pytest.raises(ValueError):
        RegimeStrategyConfig(name="x", sma_fast=40, sma_slow=10)
    with pytest.raises(ValueError):
        RegimeStrategyConfig(name="x", dd_aligned_max=7, dd_reduce=7)
    with pytest.raises(ValueError):
        RegimeStrategyConfig(name="x", exit_band_pct=-1)
