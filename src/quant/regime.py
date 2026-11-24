"""Régimen semanal y flujo institucional del S&P 500: cálculos puros (sin red, fáciles de testear).

Exposición objetivo ∈ {0, 50, 100}% en el fondo indexado (el resto en el monetario). Se decide solo al cierre
de la última sesión de cada semana (normalmente el viernes) y el traspaso tarda `execution_lag` sesiones.

Indicadores diarios (SPY):
  día de distribución (DD)   rentabilidad ≤ −dd_drop_pct y volumen > volumen de la sesión anterior
  DD25                       nº de DD en las últimas `dd_window` sesiones
  CMF20                      Chaikin Money Flow de `cmf_window` sesiones
  OBV                        On-Balance Volume
  FTD                        día 4-7 de un intento de rebote, subida ≥ ftd_gain_pct con volumen > sesión anterior
Indicadores semanales (cierre de la semana): SMA10s, SMA40s, divergencia OBV, VIX.

Cada viernes:
  ALINEADO  C > SMA40 y SMA10 > SMA40, DD25 ≤ dd_aligned_max, CMF > 0, sin divergencia OBV, VIX ≤ vix_max
  REDUCIR   C < SMA40, o DD25 ≥ dd_reduce, o CMF < 0 dos viernes seguidos, o divergencia OBV, o VIX > vix_max dos viernes seguidos
  SALIDA    C < SMA40 dos viernes seguidos y además (DD25 ≥ dd_reduce, o CMF < 0, o SMA10 < SMA40)
  100% → 0% si SALIDA; → 50% si REDUCIR.   50% → 0% si SALIDA; → 100% si ALINEADO.
  0% → 100% si ALINEADO; → 50% si hubo FTD en la semana y C > mínimo del intento, o si C > SMA40 y SMA10 > SMA40.
  50% de rebote (tras FTD): → 0% si C < mínimo del intento; deja de ser «de rebote» cuando C > SMA40.
Choque de un día: si la última sesión de la semana cae ≥ shock_day_pct, ese viernes se evalúa con los datos de la sesión
anterior (no se decide sobre un desplome aislado); la semana siguiente ya lo incluye.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

LEVELS = (0, 50, 100)


@dataclass(frozen=True)
class RegimeParams:
    sma_fast: int = 10
    sma_slow: int = 40
    dd_window: int = 25
    dd_drop_pct: float = 0.2
    dd_reduce: int = 7
    dd_aligned_max: int = 5
    cmf_window: int = 20
    obv_weeks: int = 10
    vix_max: float = 25.0
    ftd_gain_pct: float = 1.25
    ftd_first_day: int = 4
    ftd_last_day: int = 7
    warnings_to_reduce: int = 2
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
    """Lectura de un viernes: valores y condiciones de las reglas."""

    close: float
    sma_fast: float
    sma_slow: float
    dd25: int
    cmf: float
    obv_div: bool
    vix: float | None
    prev_below_slow: bool
    prev_cmf_negative: bool
    prev_vix_high: bool

    @property
    def above_slow(self) -> bool:
        return self.close > self.sma_slow

    @property
    def trend_up(self) -> bool:
        return self.close > self.sma_slow and self.sma_fast > self.sma_slow

    @property
    def trend_down(self) -> bool:
        return self.close < self.sma_slow and self.sma_fast < self.sma_slow

    def vix_high(self, p: RegimeParams) -> bool:
        return self.vix is not None and self.vix > p.vix_max

    def aligned(self, p: RegimeParams) -> bool:
        return self.trend_up and self.dd25 <= p.dd_aligned_max and self.cmf > 0 and not self.obv_div and not self.vix_high(p)

    def flow_warnings(self, p: RegimeParams) -> list[str]:
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

    def reduce_reasons(self, p: RegimeParams) -> list[str]:
        warnings = self.flow_warnings(p)
        if not self.above_slow:
            return [f"cierre bajo la SMA{p.sma_slow}s", *warnings]
        return warnings if len(warnings) >= p.warnings_to_reduce else []

    def exit_reasons(self, p: RegimeParams) -> list[str]:
        if self.above_slow or not self.prev_below_slow:
            return []
        confirm = []
        if self.dd25 >= p.dd_reduce:
            confirm.append(f"{self.dd25} días de distribución")
        if self.cmf < 0:
            confirm.append(f"CMF{p.cmf_window} negativo")
        if self.sma_fast < self.sma_slow:
            confirm.append(f"SMA{p.sma_fast}s < SMA{p.sma_slow}s")
        if not confirm:
            return []
        return [f"segundo cierre semanal seguido bajo la SMA{p.sma_slow}s", *confirm]


@dataclass
class RallyAttempt:
    """Intento de rebote (solo se sigue con exposición 0%)."""

    low: float
    day: int = 0  # 0 = esperando el día 1; >0 = día del intento
    expired: bool = False
    ftd_date: pd.Timestamp | None = None

    def update(self, row: pd.Series, prev_close: float, prev_volume: float, ts: pd.Timestamp, p: RegimeParams) -> None:
        if row["low"] < self.low:  # nuevo mínimo: el intento vuelve a empezar
            self.low, self.day, self.expired, self.ftd_date = float(row["low"]), 0, False, None
        if self.expired or self.ftd_date is not None:
            return
        if self.day == 0:
            if row["close"] > prev_close:
                self.day = 1
            return
        self.day += 1
        gain = row["close"] / prev_close - 1
        if p.ftd_first_day <= self.day <= p.ftd_last_day and gain >= p.ftd_gain_pct / 100 and row["volume"] > prev_volume:
            self.ftd_date = ts
        elif self.day >= p.ftd_last_day:
            self.expired = True


@dataclass
class Switch:
    date: pd.Timestamp  # viernes de la decisión (sesión de cierre de la semana)
    from_pct: int
    to_pct: int
    reasons: list[str]


@dataclass
class RegimeReplay:
    weekly: pd.DataFrame  # con columnas exposure y mode (decisión de cada semana completa evaluada)
    switches: list[Switch]
    exposure: int
    mode: str  # "normal" o "rebote" (50% abierto tras un FTD, aún bajo la media lenta)
    rally: RallyAttempt | None  # intento de rebote en seguimiento (solo con exposición 0%)
    rally_low: float | None  # mínimo que invalida un 50% de rebote
    ftd_pending: pd.Timestamp | None  # FTD ocurrido después de la última decisión
    daily_exposure: pd.Series  # exposición efectiva de cada sesión (tras el retraso de ejecución), en tanto por uno
    first_valid: pd.Timestamp | None = None
    last_decision: pd.Timestamp | None = None


def readings_at(weekly: pd.DataFrame, i: int, p: RegimeParams) -> Readings | None:
    """Lectura de la semana `i`; None si aún faltan indicadores (o es la primera semana)."""
    row = weekly.iloc[i]
    if i == 0 or pd.isna(row["sma_slow"]) or pd.isna(row["cmf"]) or pd.isna(row["dd25"]):
        return None
    prev = weekly.iloc[i - 1]
    return Readings(
        close=float(row["close"]), sma_fast=float(row["sma_fast"]), sma_slow=float(row["sma_slow"]), dd25=int(row["dd25"]),
        cmf=float(row["cmf"]), obv_div=bool(row["obv_div"]), vix=None if pd.isna(row["vix"]) else float(row["vix"]),
        prev_below_slow=bool(not pd.isna(prev["sma_slow"]) and prev["close"] < prev["sma_slow"]),
        prev_cmf_negative=bool(not pd.isna(prev["cmf"]) and prev["cmf"] < 0),
        prev_vix_high=bool(not pd.isna(prev["vix"]) and prev["vix"] > p.vix_max),
    )


def decide(exposure: int, mode: str, r: Readings, ftd_ok: bool, rally_low: float | None,
           p: RegimeParams) -> tuple[int, str, list[str]]:
    """Exposición y modo tras la lectura del viernes, con los motivos del cambio (vacío si no cambia)."""
    if exposure == 100:
        if reasons := r.exit_reasons(p):
            return 0, "normal", reasons
        if reasons := r.reduce_reasons(p):
            return 50, "normal", reasons
        return 100, "normal", []
    if exposure == 50 and mode == "rebote":
        if rally_low is not None and r.close < rally_low:
            return 0, "normal", [f"cierre semanal bajo el mínimo del intento de rebote ({rally_low:,.2f}): el FTD ha fallado"]
        if r.aligned(p):
            return 100, "normal", ["tendencia y flujo alineados"]
        return 50, ("normal" if r.above_slow else "rebote"), []
    if exposure == 50:
        if reasons := r.exit_reasons(p):
            return 0, "normal", reasons
        if r.aligned(p):
            return 100, "normal", ["tendencia y flujo alineados"]
        return 50, "normal", []
    if r.aligned(p):
        return 100, "normal", ["tendencia y flujo alineados"]
    if ftd_ok:
        return 50, "rebote", ["Follow-Through Day confirmado y el cierre semanal respeta el mínimo del intento de rebote"]
    if r.trend_up:
        return 50, "normal", [f"tendencia semanal recuperada (cierre > SMA{p.sma_slow}s y SMA{p.sma_fast}s > SMA{p.sma_slow}s) sin flujo alineado"]
    return 0, "normal", []


def initial_exposure(r: Readings, p: RegimeParams) -> int:
    if r.aligned(p):
        return 100
    return 50 if r.trend_up else 0


def replay(daily: pd.DataFrame, weekly: pd.DataFrame, p: RegimeParams) -> RegimeReplay:
    """Aplica las reglas sesión a sesión (intento de rebote y FTD) y semana a semana (exposición) sobre todo el histórico.

    La primera semana con todos los indicadores fija la exposición inicial sin contar como traspaso.
    """
    weekly = weekly.copy()
    exposure, mode = 0, "normal"
    rally: RallyAttempt | None = None
    rally_low: float | None = None
    ftd_pending: pd.Timestamp | None = None
    switches: list[Switch] = []
    decisions: dict[pd.Timestamp, tuple[int, str]] = {}
    week_rows = {ts: i for i, ts in enumerate(weekly.index)}
    first_valid = last_decision = None
    prev: pd.Series | None = None

    for ts, row in daily.iterrows():
        if rally is not None and prev is not None:
            had_ftd = rally.ftd_date is not None
            rally.update(row, float(prev["close"]), float(prev["volume"]), ts, p)
            if rally.ftd_date is not None and not had_ftd:
                ftd_pending = ts
        prev = row

        i = week_rows.get(ts)
        if i is None or not weekly["complete"].iloc[i]:
            continue
        r = readings_at(weekly, i, p)
        if r is None:
            continue
        if first_valid is None:
            first_valid = ts
            exposure = initial_exposure(r, p)
        else:
            ftd_ok = ftd_pending is not None and rally is not None and rally.ftd_date is not None and r.close > rally.low
            new_exposure, new_mode, reasons = decide(exposure, mode, r, ftd_ok, rally_low, p)
            if new_exposure != exposure:
                switches.append(Switch(ts, exposure, new_exposure, reasons))
            if new_exposure == 50 and new_mode == "rebote" and exposure == 0:
                rally_low = rally.low if rally else None
            elif new_mode == "normal":
                rally_low = None
            exposure, mode = new_exposure, new_mode
        ftd_pending = None
        if exposure == 0 and rally is None:
            rally = RallyAttempt(low=float(row["low"]))
        elif exposure != 0:
            rally = None
        decisions[ts] = (exposure, mode)
        last_decision = ts

    weekly["exposure"] = pd.Series({k: v[0] for k, v in decisions.items()}, dtype=float)
    weekly["mode"] = pd.Series({k: v[1] for k, v in decisions.items()}, dtype=object)
    target = weekly["exposure"].reindex(daily.index).ffill()
    effective = target.shift(p.execution_lag).fillna(0.0) / 100
    return RegimeReplay(weekly, switches, exposure, mode, rally, rally_low, ftd_pending, effective, first_valid, last_decision)


def sma_only_exposure(weekly: pd.DataFrame, daily_index: pd.DatetimeIndex, p: RegimeParams) -> pd.Series:
    """Referencia simple: 100% si el cierre semanal está sobre la SMA lenta, 0% si no (mismo retraso de ejecución)."""
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
