"""Estrategia de régimen semanal del S&P 500: salida bajista, anti-pánico, emergencia por VIX, entrada y resultado."""

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
    base = dict(close=110, sma_fast=105, sma_slow=100, sma_slow_prev=98, week_ret=0.01, rsi=55.0, vix=15.0,
                dd25=2, cmf=0.1, obv_div=False)
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


def test_below_rising_average_is_not_bear():
    # Cierre bajo la SMA40 pero con la media aún subiendo: se sigue dentro, aunque el flujo sea malo
    assert decide(100, False, readings(close=95, dd25=12, cmf=-0.3, obv_div=True), P) == (100, False, [])


def test_bear_exit():
    new, deferred, reasons = decide(100, False, readings(close=95, sma_slow_prev=101), P)
    assert (new, deferred) == (0, False) and "mercado bajista" in reasons[0]


def test_stretched_week_defers_bear_exit_once():
    bear_panic = readings(close=95, sma_slow_prev=101, week_ret=-0.05, rsi=40.0)
    assert decide(100, False, bear_panic, P) == (100, True, [])
    assert decide(100, False, readings(close=95, sma_slow_prev=101, rsi=25.0), P) == (100, True, [])
    # El viernes siguiente sale si sigue bajista, aunque vuelva a estar estirado
    new, deferred, reasons = decide(100, True, bear_panic, P)
    assert (new, deferred) == (0, False) and "aplazada" in reasons[0]
    # Y se queda si ha recuperado la media
    assert decide(100, True, readings(close=102, sma_slow_prev=101), P) == (100, False, [])


def test_vix_emergency_exits_without_waiting():
    # Media subiendo y semana de pánico: sin emergencia no saldría
    crash = readings(close=95, week_ret=-0.11, rsi=20.0, vix=40.0)
    new, _, reasons = decide(100, False, crash, P)
    assert new == 0 and "emergencia" in reasons[0]
    # VIX alto con el cierre aún sobre la media: no hay emergencia
    assert decide(100, False, readings(vix=40.0), P)[0] == 100
    # Sin VIX la emergencia queda desactivada
    assert decide(100, False, readings(close=95, week_ret=-0.11, vix=None), P)[0] == 100


def test_entry_needs_band():
    assert decide(0, False, readings(close=100.9), P)[0] == 0
    new, _, reasons = decide(0, False, readings(close=101.1), P)
    assert new == 100 and "línea de entrada" in reasons[0]


def test_flow_context_never_trades():
    assert decide(100, False, readings(dd25=15, cmf=-0.5, obv_div=True), P)[0] == 100


def test_weekly_frame_slope_and_week_return():
    p = RegimeParams(**SMALL, slope_weeks=2)
    close = [c for c in np.linspace(100, 120, 12) for _ in range(5)]
    w = weekly_frame(daily_indicators(ohlcv(close), None, p), p)
    assert w["sma_slow_prev"].iloc[-1] == pytest.approx(w["sma_slow"].iloc[-3])
    assert w["week_ret"].iloc[-1] == pytest.approx(w["close"].iloc[-1] / w["close"].iloc[-2] - 1)


def test_replay_rising_average_dip_makes_no_switch():
    p = RegimeParams(**SMALL)
    close = list(np.linspace(100, 160, 100)) + [150.0] * 5 + list(np.linspace(152, 170, 20))
    d = daily_indicators(ohlcv(close), None, p)
    rep = replay(d, weekly_frame(d, p), p)
    assert rep.switches == [] and rep.exposure == 100


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
    assert {c.id for c in res.checks} == {"emergency", "trend", "panic", "entry", "cross", "dd", "cmf", "obv"}
    assert res.sma_slow < res.enter_line and len(res.chart.enter_line) == len(res.chart.dates)
    assert next(c for c in res.checks if c.id == "emergency").status == "na"  # sin VIX la regla bajista sigue funcionando
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
        RegimeStrategyConfig(name="x", enter_band_pct=-1)
    with pytest.raises(ValueError):
        RegimeStrategyConfig(name="x", panic_rsi=0)
