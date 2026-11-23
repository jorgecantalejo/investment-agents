"""Cálculos cuantitativos puros sobre series de cierres (sin red, fáciles de testear).

Todas las funciones devuelven None cuando no hay historia suficiente:
nunca extrapolan ni rellenan huecos.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

PERIOD_MONTHS: dict[str, int] = {"1m": 1, "3m": 3, "6m": 6, "12m": 12}


def _clean(close: pd.Series) -> pd.Series:
    return close.dropna().sort_index()


def period_return(close: pd.Series, months: int) -> float | None:
    """Rentabilidad simple (%) desde el último cierre en o antes de hace `months` meses naturales."""
    close = _clean(close)
    if close.empty:
        return None
    end_date = close.index[-1]
    start_date = end_date - pd.DateOffset(months=months)
    if close.index[0] > start_date:
        return None
    start_price = close.loc[:start_date].iloc[-1]
    if start_price <= 0:
        return None
    return round(float(close.iloc[-1] / start_price - 1) * 100, 2)


def returns_by_period(close: pd.Series) -> dict[str, float | None]:
    return {label: period_return(close, months) for label, months in PERIOD_MONTHS.items()}


def simple_moving_average(close: pd.Series, window: int) -> float | None:
    """Media móvil simple de las últimas `window` observaciones."""
    close = _clean(close)
    if len(close) < window:
        return None
    return round(float(close.iloc[-window:].mean()), 4)


def drawdown_from_high(close: pd.Series, window_days: int) -> tuple[float | None, float | None, str | None]:
    """Caída (%) del último precio frente al máximo de los últimos `window_days` días naturales.

    Devuelve (drawdown_pct <= 0, máximo, fecha del máximo).
    """
    close = _clean(close)
    if close.empty:
        return None, None, None
    window = close.loc[close.index[-1] - pd.Timedelta(days=window_days):]
    high = float(window.max())
    high_date = window.idxmax().strftime("%Y-%m-%d")
    return round((float(close.iloc[-1]) / high - 1) * 100, 2), round(high, 4), high_date


def max_drawdown(close: pd.Series, window_days: int) -> float | None:
    """Peor caída pico-valle (%) dentro de la ventana."""
    close = _clean(close)
    if len(close) < 2:
        return None
    window = close.loc[close.index[-1] - pd.Timedelta(days=window_days):]
    dd = window / window.cummax() - 1
    return round(float(dd.min()) * 100, 2)


def annualized_volatility(close: pd.Series, window_days: int = 365) -> float | None:
    """Volatilidad anualizada (%) de los rendimientos diarios del último año.

    El factor de anualización se infiere del nº de observaciones por año (≈252 en
    bolsa, ≈365 en cripto), para no sesgar activos que cotizan todos los días.
    """
    close = _clean(close)
    window = close.loc[close.index[-1] - pd.Timedelta(days=window_days):] if not close.empty else close
    daily = window.pct_change().dropna()
    if len(daily) < 20:
        return None
    span_days = max((window.index[-1] - window.index[0]).days, 1)
    periods_per_year = len(daily) * 365.25 / span_days
    return round(float(daily.std(ddof=1)) * math.sqrt(periods_per_year) * 100, 2)


def relative_performance(asset: dict[str, float | None], benchmark: dict[str, float | None]) -> dict[str, float | None]:
    """Diferencia en puntos porcentuales (activo − benchmark) por periodo."""
    return {
        period: round(a - b, 2) if a is not None and b is not None else None
        for period, a in asset.items()
        for b in [benchmark.get(period)]
    }


def classify_trend(price: float, sma50: float | None, sma200: float | None, return_6m: float | None) -> str:
    """Etiqueta de tendencia determinista: 'up', 'down', 'sideways' o 'insufficient_data'."""
    if sma50 is None or sma200 is None or return_6m is None:
        return "insufficient_data"
    if price > sma200 and sma50 > sma200 and return_6m > 0:
        return "up"
    if price < sma200 and sma50 < sma200 and return_6m < 0:
        return "down"
    return "sideways"


def weekly_closes(close: pd.Series, window_days: int = 365) -> list[float]:
    """Último cierre de cada semana en la ventana, terminando siempre en el último precio (para minigráficos)."""
    close = _clean(close)
    if close.empty:
        return []
    window = close.loc[close.index[-1] - pd.Timedelta(days=window_days):]
    weekly = window.resample("W").last().dropna()
    values = [round(float(v), 4) for v in weekly]
    values[-1] = round(float(close.iloc[-1]), 4)
    return values


def is_finite(value: float | None) -> bool:
    return value is not None and bool(np.isfinite(value))


def percentile_rank(values: pd.Series, current: float) -> float:
    """Porcentaje de observaciones de `values` iguales o inferiores a `current` (0-100)."""
    return round(float((values <= current).mean() * 100), 1)


def value_months_ago(series: pd.Series, months: int) -> float | None:
    """Último valor disponible `months` meses antes del último dato de la serie."""
    target = series.index[-1] - pd.DateOffset(months=months)
    if target < series.index[0]:
        return None
    return round(float(series.asof(target)), 4)


def year_over_year_pct(series: pd.Series) -> pd.Series:
    """Variación interanual (%) de una serie mensual (p. ej. un índice de precios)."""
    monthly = series.resample("MS").last().dropna()
    return (monthly.pct_change(12, fill_method=None) * 100).dropna().round(2)
