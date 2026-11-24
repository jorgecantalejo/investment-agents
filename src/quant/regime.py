"""Régimen semanal del S&P 500: SMA40 semanal con banda de tolerancia. Cálculos puros (sin red, fáciles de testear).

Exposición objetivo ∈ {0, 100}% en el fondo indexado (el resto en el monetario). Se decide solo al cierre de la última
sesión de cada semana (normalmente el viernes) y el traspaso tarda `execution_lag` sesiones.

Cada viernes:
  SALIR    al 100%: cierre < SMA40 × (1 − exit_band_pct/100)   → 0%
  ENTRAR   al 0%:   cierre > SMA40 × (1 + enter_band_pct/100)  → 100%
  Entre las dos líneas (zona de tolerancia) se mantiene la exposición que haya: perforar la media por poco no da orden.
Choque de un día: si la última sesión de la semana cae ≥ shock_day_pct, ese viernes se evalúa con los datos de la sesión
anterior (no se decide sobre un desplome aislado); la semana siguiente ya lo incluye.

El volumen y el VIX son solo contexto (nunca dan órdenes; con traspasos de fondos son demasiado nerviosos):
  día de distribución (DD)   rentabilidad ≤ −dd_drop_pct y volumen > volumen de la sesión anterior
  DD25                       nº de DD en las últimas `dd_window` sesiones
  CMF20                      Chaikin Money Flow de `cmf_window` sesiones
  OBV                        On-Balance Volume (divergencia a `obv_weeks` semanas)
La SMA10 semanal también es contexto (el cruce 10/40 suaviza más pero llega tarde: −34% de caída máxima en 1993-2026).
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
    exit_band_pct: float = 1.5
    enter_band_pct: float = 1.0
    dd_window: int = 25
    dd_drop_pct: float = 0.2
    dd_reduce: int = 7
    dd_aligned_max: int = 5
    cmf_window: int = 20
    obv_weeks: int = 10
    vix_max: float = 25.0
    shock_day_pct: float = 4.0
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


def daily_indicators(ohlcv: pd.DataFrame, vix: pd.Series | None, p: RegimeParams) -> pd.DataFrame:
    """Añade ret, dd, acc, dd25, cmf, obv y vix (alineado con relleno hacia delante) a las velas diarias."""
    out = ohlcv.copy()
    out["ret"] = out["close"].pct_change()
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
    """Una fila por semana con los datos de su sesión de evaluación (la última, o la anterior si hubo choque).

    Columnas: eval_date, close_date, shock, close, sma_fast, sma_slow, dd25, cmf, obv_div, vix, complete.
    Las medias se calculan con los cierres semanales reales; en una semana de choque se sustituye el cierre de esa semana
    por el de la sesión anterior (ajuste exacto: SMA + (cierre_eval − cierre_real) / N).
    """
    keys = week_key(daily.index)
    last_dates = daily.index.to_series().groupby(keys.values).max()
    weekly = pd.DataFrame(index=pd.DatetimeIndex(last_dates.values, name="close_date"))
    weekly["friday"] = last_dates.index
    true_close = daily["close"].reindex(weekly.index)
    position = pd.Series(np.arange(len(daily)), index=daily.index)
    shock = daily["ret"].reindex(weekly.index) <= -p.shock_day_pct / 100
    eval_pos = position.reindex(weekly.index) - shock.astype(int)
    eval_dates = daily.index[eval_pos.clip(lower=0).to_numpy()]
    weekly["eval_date"] = eval_dates
    weekly["shock"] = shock.to_numpy()
    at_eval = daily.loc[eval_dates]
    close = at_eval["close"].to_numpy()
    weekly["close"] = close
    for col, n in (("sma_fast", p.sma_fast), ("sma_slow", p.sma_slow)):
        weekly[col] = true_close.rolling(n).mean() + (close - true_close) / n
    weekly["dd25"] = at_eval["dd25"].to_numpy()
    weekly["cmf"] = at_eval["cmf"].to_numpy()
    weekly["vix"] = at_eval["vix"].to_numpy()
    obv = pd.Series(at_eval["obv"].to_numpy(), index=weekly.index)
    price = weekly["close"]
    weekly["obv_div"] = (price > price.shift(p.obv_weeks)) & (obv < obv.shift(p.obv_weeks))
    weekly["complete"] = True
    if not last_week_complete and len(weekly):
        weekly.iloc[-1, weekly.columns.get_loc("complete")] = False
    return weekly




@dataclass
class Readings:
    """Lectura de un viernes: valores, líneas de la banda y contexto de flujo."""

    close: float
    sma_fast: float
    sma_slow: float
    dd25: int
    cmf: float
    obv_div: bool
    vix: float | None
    prev_cmf_negative: bool
    prev_vix_high: bool

    def exit_line(self, p: RegimeParams) -> float:
        return self.sma_slow * (1 - p.exit_band_pct / 100)

    def enter_line(self, p: RegimeParams) -> float:
        return self.sma_slow * (1 + p.enter_band_pct / 100)

    @property
    def gap_pct(self) -> float:
        return (self.close / self.sma_slow - 1) * 100

    @property
    def trend_up(self) -> bool:
        return self.close > self.sma_slow and self.sma_fast > self.sma_slow

    @property
    def trend_down(self) -> bool:
        return self.close < self.sma_slow and self.sma_fast < self.sma_slow

    def vix_high(self, p: RegimeParams) -> bool:
        return self.vix is not None and self.vix > p.vix_max

    def flow_warnings(self, p: RegimeParams) -> list[str]:
        """Avisos de flujo y macro: solo contexto, no cambian la exposición."""
        reasons = []
        if self.dd25 >= p.dd_reduce:
            reasons.append(f"{self.dd25} días de distribución en {p.dd_window} sesiones")
        if self.cmf < 0 and self.prev_cmf_negative:
            reasons.append(f"CMF{p.cmf_window} negativo dos viernes seguidos")
        if self.obv_div:
            reasons.append(f"divergencia OBV a {p.obv_weeks} semanas")
        if self.vix_high(p) and self.prev_vix_high:
            reasons.append(f"VIX > {p.vix_max:g} dos viernes seguidos")
        return reasons


@dataclass
class Switch:
    date: pd.Timestamp  # viernes de la decisión (sesión de cierre de la semana)
    from_pct: int
    to_pct: int
    reasons: list[str]


@dataclass
class RegimeReplay:
    weekly: pd.DataFrame  # con la columna exposure (decisión de cada semana completa evaluada)
    switches: list[Switch]
    exposure: int
    daily_exposure: pd.Series  # exposición efectiva de cada sesión (tras el retraso de ejecución), en tanto por uno
    first_valid: pd.Timestamp | None = None
    last_decision: pd.Timestamp | None = None


def readings_at(weekly: pd.DataFrame, i: int, p: RegimeParams) -> Readings | None:
    """Lectura de la semana `i`; None si aún falta la media lenta (o es la primera semana)."""
    row = weekly.iloc[i]
    if i == 0 or pd.isna(row["sma_slow"]):
        return None
    prev = weekly.iloc[i - 1]
    return Readings(
        close=float(row["close"]), sma_fast=float(row["sma_fast"]), sma_slow=float(row["sma_slow"]),
        dd25=0 if pd.isna(row["dd25"]) else int(row["dd25"]), cmf=0.0 if pd.isna(row["cmf"]) else float(row["cmf"]),
        obv_div=bool(row["obv_div"]), vix=None if pd.isna(row["vix"]) else float(row["vix"]),
        prev_cmf_negative=bool(not pd.isna(prev["cmf"]) and prev["cmf"] < 0),
        prev_vix_high=bool(not pd.isna(prev["vix"]) and prev["vix"] > p.vix_max),
    )


def decide(exposure: int, r: Readings, p: RegimeParams) -> tuple[int, list[str]]:
    """Exposición tras la lectura del viernes, con los motivos del cambio (vacío si no cambia)."""
    if exposure == 100 and r.close < r.exit_line(p):
        return 0, [f"cierre semanal {r.gap_pct:+.1f}% bajo la SMA{p.sma_slow}s: perfora la banda de salida (−{p.exit_band_pct:g}%)"]
    if exposure == 0 and r.close > r.enter_line(p):
        return 100, [f"cierre semanal {r.gap_pct:+.1f}% sobre la SMA{p.sma_slow}s: supera la banda de entrada (+{p.enter_band_pct:g}%)"]
    return exposure, []


def initial_exposure(r: Readings) -> int:
    return 100 if r.close > r.sma_slow else 0


def replay(daily: pd.DataFrame, weekly: pd.DataFrame, p: RegimeParams) -> RegimeReplay:
    """Aplica las reglas semana a semana sobre todo el histórico.

    La primera semana con la media lenta fija la exposición inicial sin contar como traspaso.
    """
    weekly = weekly.copy()
    exposure = 0
    switches: list[Switch] = []
    decisions: dict[pd.Timestamp, int] = {}
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
            new_exposure, reasons = decide(exposure, r, p)
            if new_exposure != exposure:
                switches.append(Switch(ts, exposure, new_exposure, reasons))
            exposure = new_exposure
        decisions[ts] = exposure
        last_decision = ts

    weekly["exposure"] = pd.Series(decisions, dtype=float)
    target = weekly["exposure"].reindex(daily.index).ffill()
    effective = target.shift(p.execution_lag).fillna(0.0) / 100
    return RegimeReplay(weekly, switches, exposure, effective, first_valid, last_decision)


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
