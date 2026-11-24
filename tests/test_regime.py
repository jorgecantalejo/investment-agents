"""Estrategia de régimen semanal y flujo del S&P 500: indicadores, reglas, FTD, choque de un día y resultado."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from src.config.models import RegimeStrategyConfig
from src.core.errors import InvalidTickerError
from src.market.service import OhlcResult
from src.quant.regime import (
    RallyAttempt, Readings, RegimeParams, chaikin_money_flow, daily_indicators, decide, replay, weekly_frame,
)
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
                prev_below_slow=False, prev_cmf_negative=False, prev_vix_high=False)
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


def test_decide_aligned_reduce_and_exit():
    assert decide(100, "normal", readings(), False, None, P)[0] == 100
    # Un solo aviso de flujo no baja del 100% (hacen falta 2)
    assert decide(100, "normal", readings(dd25=8), False, None, P)[0] == 100
    assert decide(100, "normal", readings(dd25=8, obv_div=True), False, None, P)[0] == 50
    # Primer cierre bajo la SMA40: 50%, nunca 0%
    first = decide(100, "normal", readings(close=95, cmf=-0.2, dd25=9), False, None, P)
    assert first[0] == 50
    # Segundo cierre seguido bajo la SMA40 con confirmación de flujo: 0%
    second = decide(100, "normal", readings(close=95, cmf=-0.2, prev_below_slow=True), False, None, P)
    assert second[0] == 0 and "segundo cierre" in second[2][0]
    # Segundo cierre bajo la media pero sin ninguna confirmación: no sale
    assert decide(50, "normal", readings(close=99, sma_fast=105, cmf=0.05, dd25=2, prev_below_slow=True), False, None, P)[0] == 50


def test_decide_reentry_paths():
    assert decide(0, "normal", readings(), False, None, P)[0] == 100
    bear = readings(close=90, sma_fast=95, cmf=-0.1, dd25=8)
    assert decide(0, "normal", bear, False, None, P)[0] == 0
    assert decide(0, "normal", bear, True, None, P)[:2] == (50, "rebote")
    # El 50% de rebote se deshace si el cierre semanal pierde el mínimo del intento
    assert decide(50, "rebote", bear, False, 92.0, P)[0] == 0
    assert decide(50, "rebote", bear, False, 85.0, P)[:2] == (50, "rebote")


def test_rally_attempt_ftd_window():
    p = RegimeParams()
    rally = RallyAttempt(low=90.0)
    closes = [91, 91.5, 91.6, 91.7, 93.0]  # día 1..5; el día 5 sube +1,4%
    prev_close, prev_vol = 90.5, 1000.0
    for i, c in enumerate(closes):
        row = pd.Series({"low": 90.5, "close": c, "volume": 1100.0 + i})
        rally.update(row, prev_close, prev_vol, pd.Timestamp("2024-01-01") + pd.Timedelta(days=i), p)
        prev_close, prev_vol = c, row["volume"]
    assert rally.day == 5 and rally.ftd_date is not None
    # Si después pierde el mínimo del intento, el FTD queda anulado
    rally.update(pd.Series({"low": 89.0, "close": 89.5, "volume": 900.0}), 93.0, 1104.0, pd.Timestamp("2024-01-08"), p)
    assert rally.ftd_date is None and rally.low == 89.0


def test_rally_attempt_resets_on_new_low_and_ignores_early_jump():
    p = RegimeParams()
    rally = RallyAttempt(low=90.0)
    rally.update(pd.Series({"low": 90.5, "close": 92.0, "volume": 2000.0}), 90.6, 1000.0, pd.Timestamp("2024-01-01"), p)
    rally.update(pd.Series({"low": 91.0, "close": 94.0, "volume": 3000.0}), 92.0, 2000.0, pd.Timestamp("2024-01-02"), p)
    assert rally.day == 2 and rally.ftd_date is None  # +2% el día 2 no es FTD
    rally.update(pd.Series({"low": 89.0, "close": 89.5, "volume": 1000.0}), 94.0, 3000.0, pd.Timestamp("2024-01-03"), p)
    assert rally.low == 89.0 and rally.day == 0


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
    assert res.exposure in (50, 100) and res.trend == "alcista"
    assert {c.id for c in res.checks} == {"trend", "dd", "cmf", "obv", "vix"}
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
