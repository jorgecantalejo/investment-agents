"""Régimen semanal del S&P 500: salir solo en mercado bajista, con salida de emergencia por pánico. Cálculos puros.

Exposición objetivo ∈ {0, 100}% en el fondo indexado (el resto en el monetario). Se decide solo al cierre de la última
sesión de cada semana (normalmente el viernes) y el traspaso tarda `execution_lag` sesiones.

Cada viernes, con C = cierre semanal y SMA40 = media de 40 cierres semanales:
  1 EMERGENCIA (al 100%)  VIX al cierre > vix_emergency y C < SMA40                       → 0% sin esperar
  2 BAJISTA    (al 100%)  C < SMA40 y SMA40 < SMA40 de hace `slope_weeks` semanas         → 0%,
               salvo semana estirada (caída semanal ≥ panic_week_drop_pct o RSI14 diario < panic_rsi): se aplaza una
               vez al viernes siguiente, que sale si sigue siendo bajista (aunque vuelva a estar estirada)
  3 ENTRADA    (al 0%)    C > SMA40 × (1 + enter_band_pct/100)                            → 100%
  4 REENTRADA RÁPIDA (al 0% tras una salida de emergencia)  C > SMA10                       → 100%
Con 1 se cubren los desplomes rápidos (marzo de 2020), con 2 los mercados bajistas lentos (2000-02, 2008, 2022). La 4 evita
quedarse fuera de una caída en V hasta que el precio supere la SMA40: tras un pánico la SMA40 queda muy por encima.

Solo contexto (no dan órdenes): días de distribución (rentabilidad ≤ −dd_drop_pct con volumen mayor que la
sesión anterior, contados en `dd_window` sesiones), CMF y divergencia OBV.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

LEVELS = (0, 100)


@dataclass(frozen=True)
class RegimeParams:
    sma_fast: int = 10
    sma_slow: int = 40
    slope_weeks: int = 4
    enter_band_pct: float = 1.0
    vix_emergency: float = 35.0
    panic_week_drop_pct: float = 3.5
    panic_rsi: float = 30.0
    fast_reentry_after_emergency: bool = True
    rsi_window: int = 14
    dd_window: int = 25
    dd_drop_pct: float = 0.2
    dd_reduce: int = 7
    dd_aligned_max: int = 5
    cmf_window: int = 20
    obv_weeks: int = 10
    execution_lag: int = 2
    cash_rate_pct: float = 0.0


def chaikin_money_flow(ohlcv: pd.DataFrame, window: int) -> pd.Series:
    span = ohlcv["high"] - ohlcv["low"]
    mfm = ((ohlcv["close"] - ohlcv["low"]) - (ohlcv["high"] - ohlcv["close"])) / span.where(span > 0)
    mfv = mfm.fillna(0.0) * ohlcv["volume"]
    return mfv.rolling(window).sum() / ohlcv["volume"].rolling(window).sum()


def on_balance_volume(ohlcv: pd.DataFrame) -> pd.Series:
    direction = np.sign(ohlcv["close"].diff()).fillna(0.0)
    return (direction * ohlcv["volume"]).cumsum()


def rsi_wilder(close: pd.Series, window: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / window, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / window, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)


def daily_indicators(ohlcv: pd.DataFrame, vix: pd.Series | None, p: RegimeParams) -> pd.DataFrame:
    """Añade ret, rsi, dd, acc, dd25, cmf, obv y vix (alineado con relleno hacia delante) a las velas diarias."""
    out = ohlcv.copy()
    out["ret"] = out["close"].pct_change()
    out["rsi"] = rsi_wilder(out["close"], p.rsi_window)
    more_volume = out["volume"] > out["volume"].shift(1)
    out["dd"] = (out["ret"] <= -p.dd_drop_pct / 100) & more_volume
    out["acc"] = (out["ret"] >= p.dd_drop_pct / 100) & more_volume
    out["dd25"] = out["dd"].astype(int).rolling(p.dd_window).sum()
    out["cmf"] = chaikin_money_flow(out, p.cmf_window)
    out["obv"] = on_balance_volume(out)
    out["vix"] = vix.reindex(out.index, method="ffill") if vix is not None and len(vix) else np.nan
    return out


def week_key(index: pd.DatetimeIndex) -> pd.Series:
    """Viernes de la semana de cada sesión (la semana bursátil termina el viernes aunque sea festivo)."""
    return pd.Series(index + pd.to_timedelta((4 - index.day_of_week) % 7, unit="D"), index=index)


def weekly_frame(daily: pd.DataFrame, p: RegimeParams, last_week_complete: bool = True) -> pd.DataFrame:
    """Una fila por semana con los datos de su última sesión.

    Columnas: friday, close, week_ret, sma_fast, sma_slow, sma_slow_prev, rsi, vix, dd25, cmf, obv_div, complete.
    """
    keys = week_key(daily.index)
    last_dates = daily.index.to_series().groupby(keys.values).max()
    weekly = pd.DataFrame(index=pd.DatetimeIndex(last_dates.values, name="close_date"))
    weekly["friday"] = last_dates.index
    at = daily.loc[weekly.index]
    weekly["close"] = at["close"].to_numpy()
    weekly["week_ret"] = weekly["close"].pct_change()
    weekly["sma_fast"] = weekly["close"].rolling(p.sma_fast).mean()
    weekly["sma_slow"] = weekly["close"].rolling(p.sma_slow).mean()
    weekly["sma_slow_prev"] = weekly["sma_slow"].shift(p.slope_weeks)
    for col in ("rsi", "vix", "dd25", "cmf"):
        weekly[col] = at[col].to_numpy()
    obv = pd.Series(at["obv"].to_numpy(), index=weekly.index)
    price = weekly["close"]
    weekly["obv_div"] = (price > price.shift(p.obv_weeks)) & (obv < obv.shift(p.obv_weeks))
    weekly["complete"] = True
    if not last_week_complete and len(weekly):
        weekly.iloc[-1, weekly.columns.get_loc("complete")] = False
    return weekly


@dataclass
class Readings:
    """Lectura de un viernes: valores de las reglas y contexto de flujo."""

    close: float
    sma_fast: float
    sma_slow: float
    sma_slow_prev: float
    week_ret: float | None
    rsi: float | None
    vix: float | None
    dd25: int
    cmf: float
    obv_div: bool

    def enter_line(self, p: RegimeParams) -> float:
        return self.sma_slow * (1 + p.enter_band_pct / 100)

    @property
    def gap_pct(self) -> float:
        return (self.close / self.sma_slow - 1) * 100

    @property
    def below(self) -> bool:
        return self.close < self.sma_slow

    @property
    def slope_down(self) -> bool:
        return self.sma_slow < self.sma_slow_prev

    @property
    def bear(self) -> bool:
        return self.below and self.slope_down

    @property
    def trend_up(self) -> bool:
        return self.close > self.sma_slow and self.sma_fast > self.sma_slow

    @property
    def trend_down(self) -> bool:
        return self.close < self.sma_slow and self.sma_fast < self.sma_slow

    def emergency(self, p: RegimeParams) -> bool:
        return self.below and self.vix is not None and self.vix > p.vix_emergency

    def stretched(self, p: RegimeParams) -> bool:
        """Semana de pánico: vender aquí suele ser vender en el suelo del rebote técnico."""
        return ((self.week_ret is not None and self.week_ret <= -p.panic_week_drop_pct / 100)
                or (self.rsi is not None and self.rsi < p.panic_rsi))


@dataclass
class Switch:
    date: pd.Timestamp  # viernes de la decisión (sesión de cierre de la semana)
    from_pct: int
    to_pct: int
    reasons: list[str]


@dataclass
class RegimeReplay:
    weekly: pd.DataFrame  # con las columnas exposure y mode (decisión de cada semana completa evaluada)
    switches: list[Switch]
    exposure: int
    mode: str  # MODES: "aplazada" (al 100% con salida bajista pendiente), "emergencia" (al 0% tras una salida de emergencia)
    daily_exposure: pd.Series  # exposición efectiva de cada sesión (tras el retraso de ejecución), en tanto por uno
    first_valid: pd.Timestamp | None = None
    last_decision: pd.Timestamp | None = None


def _num(value) -> float | None:
    return None if pd.isna(value) else float(value)


def readings_at(weekly: pd.DataFrame, i: int, p: RegimeParams) -> Readings | None:
    """Lectura de la semana `i`; None si aún falta la media lenta o su pendiente."""
    row = weekly.iloc[i]
    if pd.isna(row["sma_slow"]) or pd.isna(row["sma_slow_prev"]):
        return None
    return Readings(
        close=float(row["close"]), sma_fast=float(row["sma_fast"]), sma_slow=float(row["sma_slow"]),
        sma_slow_prev=float(row["sma_slow_prev"]), week_ret=_num(row["week_ret"]), rsi=_num(row["rsi"]), vix=_num(row["vix"]),
        dd25=0 if pd.isna(row["dd25"]) else int(row["dd25"]), cmf=0.0 if pd.isna(row["cmf"]) else float(row["cmf"]),
        obv_div=bool(row["obv_div"]),
    )


MODES = ("normal", "aplazada", "emergencia")


def decide(exposure: int, mode: str, r: Readings, p: RegimeParams) -> tuple[int, str, list[str]]:
    """Exposición y modo tras la lectura del viernes, con los motivos del cambio (vacío si no cambia)."""
    slow, fast = f"SMA{p.sma_slow}s", f"SMA{p.sma_fast}s"
    if exposure == 100:
        if r.emergency(p):
            return 0, "emergencia", [f"salida de emergencia: VIX {r.vix:.1f} > {p.vix_emergency:g} con el cierre {r.gap_pct:+.1f}% bajo la {slow}"]
        if mode == "aplazada":
            if r.bear:
                return 0, "normal", [f"salida aplazada confirmada: el cierre sigue {r.gap_pct:+.1f}% bajo la {slow} y la media baja"]
            return 100, "normal", []
        if r.bear:
            if r.stretched(p):
                return 100, "aplazada", []
            return 0, "normal", [f"mercado bajista: cierre {r.gap_pct:+.1f}% bajo la {slow} y la media más baja que hace {p.slope_weeks} semanas"]
        return 100, "normal", []
    if r.close > r.enter_line(p):
        return 100, "normal", [f"cierre {r.gap_pct:+.1f}% sobre la {slow}: supera la línea de entrada (+{p.enter_band_pct:g}%)"]
    if mode == "emergencia" and p.fast_reentry_after_emergency and r.close > r.sma_fast:
        return 100, "normal", [f"reentrada rápida tras la emergencia: el cierre ({r.close:,.2f}) supera la {fast} ({r.sma_fast:,.2f})"]
    return 0, mode, []


def initial_exposure(r: Readings) -> int:
    return 100 if r.close > r.sma_slow else 0


def replay(daily: pd.DataFrame, weekly: pd.DataFrame, p: RegimeParams) -> RegimeReplay:
    """Aplica las reglas semana a semana sobre todo el histórico.

    La primera semana con la media lenta y su pendiente fija la exposición inicial sin contar como traspaso.
    """
    weekly = weekly.copy()
    exposure, mode = 0, "normal"
    switches: list[Switch] = []
    decisions: dict[pd.Timestamp, tuple[int, str]] = {}
    first_valid = last_decision = None
    for i, ts in enumerate(weekly.index):
        if not weekly["complete"].iloc[i]:
            continue
        r = readings_at(weekly, i, p)
        if r is None:
            continue
        if first_valid is None:
            first_valid, exposure = ts, initial_exposure(r)
        else:
            new_exposure, mode, reasons = decide(exposure, mode, r, p)
            if new_exposure != exposure:
                switches.append(Switch(ts, exposure, new_exposure, reasons))
            exposure = new_exposure
        decisions[ts] = (exposure, mode)
        last_decision = ts

    weekly["exposure"] = pd.Series({k: v[0] for k, v in decisions.items()}, dtype=float)
    weekly["mode"] = pd.Series({k: v[1] for k, v in decisions.items()}, dtype=object)
    target = weekly["exposure"].reindex(daily.index).ffill()
    effective = target.shift(p.execution_lag).fillna(0.0) / 100
    return RegimeReplay(weekly, switches, exposure, mode, effective, first_valid, last_decision)



def sma_only_exposure(weekly: pd.DataFrame, daily_index: pd.DatetimeIndex, p: RegimeParams) -> pd.Series:
    """Referencia sin banda: 100% si el cierre semanal está sobre la SMA lenta, 0% si no (mismo retraso de ejecución)."""
    complete = weekly[weekly["complete"] & weekly["sma_slow"].notna()]
    target = (complete["close"] > complete["sma_slow"]).astype(float).reindex(daily_index).ffill()
    return target.shift(p.execution_lag).fillna(0.0)


def performance(daily: pd.DataFrame, exposures: dict[str, pd.Series], p: RegimeParams, since: pd.Timestamp) -> dict[str, dict[str, float]]:
    """CAGR, volatilidad, máxima caída y peor año de cada serie de exposición y de comprar y mantener, desde `since`.

    Rentabilidad diaria = exposición al cierre anterior × rentabilidad de SPY (con dividendos) + resto × monetario.
    """
    ret = daily["ret"].loc[since:].fillna(0.0)
    cash = (1 + p.cash_rate_pct / 100) ** (1 / 252) - 1
    series, held_by = {"buy_hold": ret}, {}
    for name, exposure in exposures.items():
        held = exposure.loc[since:].shift(1).fillna(0.0)
        held_by[name] = held
        series[name] = held * ret + (1 - held) * cash
    out = {}
    for name, r in series.items():
        equity = (1 + r).cumprod()
        years = max(len(r) / 252, 1e-9)
        drawdown = equity / equity.cummax() - 1
        yearly = (1 + r).groupby(r.index.year).prod() - 1
        out[name] = {
            "cagr_pct": round(float(equity.iloc[-1] ** (1 / years) - 1) * 100, 2),
            "vol_pct": round(float(r.std() * np.sqrt(252) * 100), 2),
            "max_dd_pct": round(float(drawdown.min() * 100), 2),
            "worst_year_pct": round(float(yearly.min() * 100), 2),
            "total_pct": round(float((equity.iloc[-1] - 1) * 100), 1),
            "avg_exposure_pct": round(float(held_by[name].mean() * 100), 1) if name in held_by else 100.0,
        }
    return out
