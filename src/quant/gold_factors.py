"""Factores macro del oro: estado actual de cada factor y qué hizo históricamente el oro en EUR según el factor subiera o bajara.

Es un estudio informativo: no modifica las señales de la estrategia de tendencia. Todo se calcula en Python con datos
públicos (FRED y Yahoo Finance) y cada factor falla de forma independiente.

Método (mensual, fin de mes):
- Variación del factor en los 3 meses previos: diferencia en puntos (tipos, inflación, VIX) o % (precios).
- Régimen «sube» o «baja» según el signo, ignorando variaciones dentro de una banda plana.
- Rentabilidad posterior del oro en EUR a 1 y 3 meses en cada régimen, con nº de meses y % de veces que subió.
- Las series mensuales (IPC) se desplazan un mes por el retraso de publicación, para no usar datos que aún no se conocían.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

import pandas as pd

from src.core.cache import FileCache
from src.core.errors import MarketDataError
from src.market.providers import fred
from src.market.service import MarketDataService
from src.models import FactorRegime, FactorStudy, MacroFactor
from src.quant import metrics
from src.quant.indicators import _cached

logger = logging.getLogger(__name__)

LOOKBACK_MONTHS = 3      # Variación del factor que define el régimen
MIN_MONTHS = 24          # Meses mínimos en un régimen para dar un veredicto
EDGE_PP = 1.0            # Diferencia mínima de rentabilidad media a 3m (pp) para hablar de factor favorable o desfavorable
MIN_T = 1.5              # Solidez mínima de esa diferencia (estadístico t con muestra efectiva)
CORR_WEEKS = 52
FRED_START = date(2000, 1, 1)
GOLD_USD, EURUSD = "GC=F", "EURUSD=X"


@dataclass(frozen=True)
class FactorSpec:
    id: str
    name: str
    provider: Literal["fred", "yahoo"]
    series: str
    transform: Literal["level", "yoy"]
    change: Literal["diff", "pct"]
    flat_band: float
    unit: str
    usual: str
    usual_sign: Literal[-1, 1]   # signo habitual de la relación: +1 = factor al alza, oro al alza
    note: str = ""

    @property
    def monthly(self) -> bool:
        return self.transform == "yoy"

    @property
    def change_unit(self) -> str:
        if self.change == "pct":
            return "%"
        return "pb" if self.unit == "%" else "pts"

    @property
    def source(self) -> str:
        return f"FRED ({self.series})" if self.provider == "fred" else f"Yahoo Finance ({self.series})"


GOLD_FACTORS: list[FactorSpec] = [
    FactorSpec("us_real_10y", "Tipo real EE. UU. 10a", "fred", "DFII10", "level", "diff", 0.05, "%",
               "Tipos reales al alza → oro a la baja: sube el coste de oportunidad de tener un activo sin rentas.", -1),
    FactorSpec("fed_funds", "Tipo Fed", "fred", "DFF", "level", "diff", 0.05, "%",
               "Subidas de tipos → oro a la baja; bajadas → oro al alza.", -1),
    FactorSpec("us_breakeven_10y", "Inflación esperada 10a", "fred", "T10YIE", "level", "diff", 0.05, "%",
               "Más inflación esperada → oro al alza, como refugio frente a la pérdida de poder adquisitivo.", 1),
    FactorSpec("us_cpi_yoy", "IPC EE. UU. (interanual)", "fred", "CPIAUCSL", "yoy", "diff", 0.1, "%",
               "Inflación al alza → oro al alza.", 1, "Dato mensual, desplazado un mes por el retraso de publicación."),
    FactorSpec("ez_hicp_yoy", "IPCA zona euro (interanual)", "fred", "CP0000EZ19M086NEST", "yoy", "diff", 0.1, "%",
               "Inflación al alza → oro al alza.", 1, "Dato mensual, desplazado un mes por el retraso de publicación."),
    FactorSpec("dxy", "Índice dólar (DXY)", "yahoo", "DX-Y.NYB", "level", "pct", 0.5, "",
               "Dólar fuerte → oro en USD a la baja. En EUR el efecto se compensa en parte por el tipo de cambio.", -1),
    FactorSpec("eurusd", "EUR/USD", "yahoo", "EURUSD=X", "level", "pct", 0.5, "",
               "Euro al alza → oro en EUR a la baja: el oro cotiza en dólares y cada dólar vale menos euros.", -1),
    FactorSpec("vix", "VIX", "yahoo", "^VIX", "level", "diff", 0.5, "",
               "Más tensión en bolsa → demanda de refugio y oro al alza (aunque en pánicos agudos se vende para cubrir pérdidas).", 1),
    FactorSpec("hy_spread", "Diferencial high yield", "fred", "BAMLH0A0HYM2", "level", "diff", 0.05, "%",
               "Estrés crediticio al alza → oro al alza como refugio.", 1, "FRED solo publica los últimos 3 años de esta serie."),
    FactorSpec("oil", "Petróleo WTI", "yahoo", "CL=F", "level", "pct", 0.5, "",
               "Petróleo al alza → más inflación esperada y oro al alza.", 1),
    FactorSpec("silver", "Plata", "yahoo", "SI=F", "level", "pct", 0.5, "",
               "Se mueve en la misma dirección que el oro (metal precioso con más componente industrial).", 1),
]


def month_end(series: pd.Series) -> pd.Series:
    return series.resample("ME").last().dropna()


def change(series: pd.Series, spec: FactorSpec, periods: int) -> pd.Series:
    if spec.change == "pct":
        return (series.pct_change(periods, fill_method=None) * 100).dropna()
    return series.diff(periods).dropna()


def regimes(change_3m: pd.Series, spec: FactorSpec) -> tuple[pd.Series, pd.Series]:
    return change_3m > spec.flat_band, change_3m < -spec.flat_band


def _regime(fwd1: pd.Series, fwd3: pd.Series, mask: pd.Series) -> FactorRegime:
    r1, r3 = fwd1[mask].dropna(), fwd3[mask].dropna()
    if r3.empty:
        return FactorRegime(months=0)
    return FactorRegime(months=len(r3), avg_return_1m_pct=round(float(r1.mean()), 2) if len(r1) else None,
                        avg_return_3m_pct=round(float(r3.mean()), 2), up_share_3m_pct=round(float((r3 > 0).mean() * 100), 1),
                        std_3m_pct=round(float(r3.std()), 2) if len(r3) > 1 else None)


def forward_returns(gold_m: pd.Series) -> tuple[pd.Series, pd.Series]:
    return (gold_m.shift(-1) / gold_m - 1) * 100, (gold_m.shift(-3) / gold_m - 1) * 100


def regime_stats(gold_m: pd.Series, factor_m: pd.Series, spec: FactorSpec) -> tuple[FactorRegime, FactorRegime]:
    """Rentabilidad posterior del oro (mensual) cuando el factor subía y cuando bajaba en los 3 meses previos."""
    fwd1, fwd3 = forward_returns(gold_m)
    ch = change(factor_m, spec, LOOKBACK_MONTHS).reindex(gold_m.index)
    up, down = regimes(ch, spec)
    return _regime(fwd1, fwd3, up), _regime(fwd1, fwd3, down)


def t_stat(rising: FactorRegime, falling: FactorRegime) -> float | None:
    """Diferencia de medias a 3m entre regímenes / error estándar. Muestra efectiva = meses / 3 (ventanas solapadas)."""
    if None in (rising.std_3m_pct, falling.std_3m_pct, rising.avg_return_3m_pct, falling.avg_return_3m_pct):
        return None
    n_up, n_down = rising.months / LOOKBACK_MONTHS, falling.months / LOOKBACK_MONTHS
    se = (rising.std_3m_pct ** 2 / n_up + falling.std_3m_pct ** 2 / n_down) ** 0.5  # type: ignore[operator]
    return round((rising.avg_return_3m_pct - falling.avg_return_3m_pct) / se, 2) if se else None  # type: ignore[operator]


def verdict(direction: str, rising: FactorRegime, falling: FactorRegime) -> tuple[float | None, float | None, int | None, str]:
    """(ventaja a 3m del régimen actual en pp, estadístico t, signo observado de la relación, señal).

    Solo hay «favorable» o «desfavorable» si la diferencia supera EDGE_PP y es sólida (|t| ≥ MIN_T)."""
    both = rising.months >= MIN_MONTHS and falling.months >= MIN_MONTHS
    if not both or rising.avg_return_3m_pct is None or falling.avg_return_3m_pct is None:
        return None, None, None, "insufficient_data"
    spread = rising.avg_return_3m_pct - falling.avg_return_3m_pct
    t = t_stat(rising, falling)
    solid = t is not None and abs(t) >= MIN_T and abs(spread) >= EDGE_PP
    observed = (1 if spread > 0 else -1) if solid else 0
    if direction not in ("up", "down"):
        return None, t, observed, "neutral" if direction == "flat" else "insufficient_data"
    edge = round(spread if direction == "up" else -spread, 2)
    signal = ("favorable" if edge > 0 else "unfavorable") if solid else "neutral"
    return edge, t, observed, signal


def weekly_correlation(gold: pd.Series, factor: pd.Series, spec: FactorSpec, weeks: int = CORR_WEEKS) -> float | None:
    """Correlación de las variaciones semanales del oro y del factor en las últimas `weeks` semanas."""
    g = gold.resample("W-FRI").last().pct_change(fill_method=None)
    f = change(factor.resample("W-FRI").last(), spec, 1)
    pairs = pd.concat([g, f], axis=1, join="inner").dropna().iloc[-weeks:]
    if len(pairs) < weeks * 0.75:
        return None
    return round(float(pairs.iloc[:, 0].corr(pairs.iloc[:, 1])), 2)


def gold_in_eur(service: MarketDataService) -> pd.Series:
    """Oro en EUR por onza: futuro COMEX ÷ EUR/USD. Replica PPFB.DE con mucha más historia (desde 2003)."""
    gold = service.get_ohlc(GOLD_USD, "max").ohlc["close"]
    fx = service.get_ohlc(EURUSD, "max").ohlc["close"]
    eur = (gold / fx.reindex(gold.index, method="ffill")).dropna()
    eur = eur[eur > 0]
    if len(eur) < 500:
        raise MarketDataError(f"Historia insuficiente del oro en EUR: {len(eur)} sesiones")
    return eur


def factor_series(spec: FactorSpec, service: MarketDataService, cache: FileCache | None) -> pd.Series:
    """Serie del factor ya transformada: diaria (fin de mes en las mensuales, desplazada un mes por publicación)."""
    if spec.provider == "yahoo":
        close = service.get_ohlc(spec.series, "max").ohlc["close"]
        return close[close > 0] if spec.change == "pct" else close
    if cache is None:
        raise MarketDataError("Datos macro desactivados (data.macro_data: false)")
    raw = _cached(cache, f"fred_full_{spec.series}",
                  lambda: {d.strftime("%Y-%m-%d"): float(v) for d, v in fred.fetch_series(spec.series, FRED_START).items()})
    if not raw:
        raise MarketDataError(f"FRED {spec.series} no disponible")
    series = pd.Series(raw, dtype=float)
    series.index = pd.to_datetime(series.index)
    series = series.sort_index()
    if spec.transform == "yoy":
        yoy = metrics.year_over_year_pct(series)
        yoy.index = yoy.index + pd.offsets.MonthEnd(0) + pd.offsets.MonthEnd(1)  # conocido al final del mes siguiente
        return yoy
    return series


def evaluate_factor(spec: FactorSpec, gold: pd.Series, gold_m: pd.Series, series: pd.Series) -> MacroFactor:
    factor_m = series if spec.monthly else month_end(series)
    rising, falling = regime_stats(gold_m, factor_m, spec)
    value = float(series.iloc[-1])
    if spec.monthly:
        past = series.iloc[-1 - LOOKBACK_MONTHS] if len(series) > LOOKBACK_MONTHS else None
    else:
        past = metrics.value_months_ago(series, LOOKBACK_MONTHS)
    if past is None:
        change_3m, direction = None, "insufficient_data"
    else:
        change_3m = (value / past - 1) * 100 if spec.change == "pct" else (value - past) * (100 if spec.change_unit == "pb" else 1)
        band = spec.flat_band * (100 if spec.change_unit == "pb" else 1)
        direction = "up" if change_3m > band else "down" if change_3m < -band else "flat"
        change_3m = round(change_3m, 2)
    edge, t, observed, signal = verdict(direction, rising, falling)
    return MacroFactor(
        id=spec.id, name=spec.name, source=spec.source, unit=spec.unit, change_unit=spec.change_unit, usual=spec.usual,
        usual_sign=spec.usual_sign, value=round(value, 4), as_of=series.index[-1].strftime("%Y-%m-%d"), change_3m=change_3m,
        direction=direction, rising=rising, falling=falling, edge_3m_pp=edge, t_stat=t, observed_sign=observed,  # type: ignore[arg-type]
        corr_52w=None if spec.monthly else weekly_correlation(gold, series, spec), signal=signal,  # type: ignore[arg-type]
        sparkline=[round(float(v), 4) for v in series.iloc[-13:]] if spec.monthly else metrics.weekly_closes(series),
        note=spec.note,
    )


def method_notes() -> list[str]:
    return [
        f"Cada fin de mes se mira si el factor subió o bajó en los {LOOKBACK_MONTHS} meses anteriores y qué hizo el oro en EUR "
        "el mes y el trimestre siguientes. Las variaciones muy pequeñas (banda plana) no cuentan en ningún régimen.",
        f"«Favorece» o «perjudica» compara la rentabilidad media a 3 meses del régimen actual con la del contrario. Hace falta "
        f"una diferencia de al menos {EDGE_PP:g} pp, {MIN_MONTHS} meses en cada régimen y una solidez |t| ≥ {MIN_T:g}; si no, «sin efecto claro».",
        "La solidez (t) divide la diferencia de medias por su error estándar contando un tercio de los meses, porque las "
        "ventanas de 3 meses se solapan. |t| < 1,5: puede ser ruido; 1,5-2: indicio; > 2: diferencia consistente.",
        "La correlación usa variaciones semanales de las últimas 52 semanas (no se calcula en datos mensuales).",
        "Es estadística descriptiva del pasado, no una relación causal ni una previsión. No altera las señales de la estrategia.",
    ]


def build_factor_study(service: MarketDataService, macro_cache: FileCache | None, specs: list[FactorSpec] | None = None,
                       now: datetime | None = None) -> FactorStudy:
    """Estudio completo. Nunca lanza: un fallo del oro queda en `error` y el de un factor en su propio `error`."""
    generated_at = (now or datetime.now()).isoformat(timespec="seconds")
    study = FactorStudy(asset_name="Oro", price_basis="Oro en EUR (futuro COMEX GC=F ÷ EUR/USD), equivalente a PPFB.DE",
                        method=method_notes(), generated_at=generated_at)
    try:
        gold = gold_in_eur(service)
    except MarketDataError as exc:
        logger.error("Sin precio del oro en EUR para el estudio de factores: %s", exc)
        return study.model_copy(update={"error": str(exc)})
    gold_m = month_end(gold)
    _, fwd3 = forward_returns(gold_m)
    fwd3 = fwd3.dropna()
    study.since, study.until, study.months = gold_m.index[0].strftime("%Y-%m"), gold.index[-1].strftime("%Y-%m-%d"), len(fwd3)
    study.base_avg_3m_pct = round(float(fwd3.mean()), 2)
    study.base_up_share_3m_pct = round(float((fwd3 > 0).mean() * 100), 1)

    for spec in specs or GOLD_FACTORS:
        try:
            factor = evaluate_factor(spec, gold, gold_m, factor_series(spec, service, macro_cache))
        except (MarketDataError, KeyError, IndexError, ValueError) as exc:
            logger.warning("Factor %s no disponible: %s", spec.id, exc)
            factor = MacroFactor(id=spec.id, name=spec.name, source=spec.source, unit=spec.unit, change_unit=spec.change_unit,
                                 usual=spec.usual, usual_sign=spec.usual_sign, note=spec.note, error=str(exc))
        study.factors.append(factor)
    study.favorable = sum(f.signal == "favorable" for f in study.factors)
    study.unfavorable = sum(f.signal == "unfavorable" for f in study.factors)
    study.neutral = sum(f.signal == "neutral" for f in study.factors)
    return study
