"""Seguimiento de tendencia sobre velas diarias: cálculos puros (sin red, fáciles de testear).

Reglas (todas evaluadas al cierre; la orden se ejecuta en la apertura de la sesión siguiente):
  filtro   cierre > media simple de `filter_sma` sesiones
  entrada  cierre > máximo de las `entry_breakout` sesiones previas (sin contar la actual)
  stop     precio de entrada − `atr_multiple` × ATR(`atr_window`) del día de la señal
  salida   cierre <= stop inicial, o cierre < mínimo de las `exit_low` sesiones previas
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.models import TrendTrade


@dataclass(frozen=True)
class TrendParams:
    filter_sma: int = 200
    entry_breakout: int = 20
    exit_low: int = 10
    atr_window: int = 20
    atr_multiple: float = 2.0
    capital: float = 10000.0
    risk_pct: float = 1.0
    commission_pct: float = 0.1
    commission_min: float = 0.0
    spread_pct: float = 0.05

    @property
    def risk_budget(self) -> float:
        return self.capital * self.risk_pct / 100


def average_true_range(ohlc: pd.DataFrame, window: int) -> pd.Series:
    """ATR con el suavizado de Wilder (media exponencial con alpha = 1/window)."""
    prev_close = ohlc["close"].shift(1)
    true_range = pd.concat(
        [ohlc["high"] - ohlc["low"], (ohlc["high"] - prev_close).abs(), (ohlc["low"] - prev_close).abs()], axis=1
    ).max(axis=1)
    return true_range.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()


def indicators(ohlc: pd.DataFrame, p: TrendParams) -> pd.DataFrame:
    """Añade sma, upper (máximo previo), lower (mínimo previo) y atr. NaN donde aún no hay historia suficiente."""
    out = ohlc.copy()
    out["sma"] = ohlc["close"].rolling(p.filter_sma).mean()
    out["upper"] = ohlc["high"].shift(1).rolling(p.entry_breakout).max()
    out["lower"] = ohlc["low"].shift(1).rolling(p.exit_low).min()
    out["atr"] = average_true_range(ohlc, p.atr_window)
    return out


def side_cost(notional: float, p: TrendParams) -> float:
    """Coste de una orden: comisión (con mínimo) + medio diferencial."""
    if notional <= 0:
        return 0.0
    return max(p.commission_min, notional * p.commission_pct / 100) + notional * p.spread_pct / 200


def position_size(entry: float, stop: float, p: TrendParams) -> tuple[int, float, bool]:
    """Máximo de participaciones cuyo riesgo hasta el stop, con costes de ida y vuelta, cabe en el presupuesto de riesgo.

    Nunca supera el capital (sin apalancamiento). Devuelve (participaciones, costes, ¿limitado por capital?).
    """
    risk_per_share = entry - stop
    if entry <= 0 or risk_per_share <= 0:
        return 0, 0.0, False
    by_capital = math.floor(p.capital / entry)
    shares = min(math.floor(p.risk_budget / risk_per_share), by_capital)

    def costs(n: int) -> float:
        return side_cost(n * entry, p) + side_cost(n * stop, p)

    while shares > 0 and shares * risk_per_share + costs(shares) > p.risk_budget:
        shares -= 1
    capped = shares == by_capital and by_capital * risk_per_share + costs(by_capital) < p.risk_budget
    return shares, round(costs(shares), 2), capped


@dataclass
class Replay:
    trades: list[TrendTrade]
    pending_entry: bool  # señal de entrada en la última sesión, aún sin ejecutar
    pending_exit: str | None  # motivo de la señal de salida en la última sesión
    sessions_in_market: int
    first_valid: pd.Timestamp | None


def replay(frame: pd.DataFrame, p: TrendParams) -> Replay:
    """Aplica las reglas sesión a sesión sobre `indicators(...)` y reconstruye la posición teórica."""
    trades: list[TrendTrade] = []
    position: TrendTrade | None = None
    entry_signal: tuple[pd.Timestamp, float] | None = None
    exit_signal: tuple[pd.Timestamp, str] | None = None
    in_market = 0
    valid = frame[["sma", "upper", "lower", "atr"]].notna().all(axis=1)
    first_valid = valid.idxmax() if valid.any() else None

    for ts, row in frame.iterrows():
        day = ts.strftime("%Y-%m-%d")
        if entry_signal is not None:  # ejecución en la apertura
            signal_ts, atr = entry_signal
            entry = float(row["open"])
            stop = entry - p.atr_multiple * atr
            shares, _, _ = position_size(entry, stop, p)
            position = TrendTrade(signal_date=signal_ts.strftime("%Y-%m-%d"), entry_date=day, entry_price=round(entry, 4),
                                  initial_stop=round(stop, 4), atr_at_signal=round(atr, 4), shares=shares)
            trades.append(position)
            entry_signal = None
        elif exit_signal is not None and position is not None:
            signal_ts, reason = exit_signal
            exit_price = float(row["open"])
            risk = position.entry_price - position.initial_stop
            position.exit_signal_date = signal_ts.strftime("%Y-%m-%d")
            position.exit_date = day
            position.exit_price = round(exit_price, 4)
            position.exit_reason = reason  # type: ignore[assignment]
            position.r_multiple = round((exit_price - position.entry_price) / risk, 2) if risk > 0 else None
            position.return_pct = round((exit_price / position.entry_price - 1) * 100, 2)
            position, exit_signal = None, None

        if position is not None:
            in_market += 1
        if not valid[ts]:
            continue
        close = float(row["close"])
        if position is None:
            if close > row["sma"] and close > row["upper"]:
                entry_signal = (ts, float(row["atr"]))
        elif close <= position.initial_stop:
            exit_signal = (ts, "stop")
        elif close < row["lower"]:
            exit_signal = (ts, "minimo")

    return Replay(trades, entry_signal is not None, exit_signal[1] if exit_signal else None, in_market, first_valid)


def round_or_none(value: float | None, digits: int = 4) -> float | None:
    if value is None or not np.isfinite(value):
        return None
    return round(float(value), digits)
