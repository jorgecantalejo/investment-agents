"""Indicadores globales de mercado para la cabecera del informe y el agente macro.

Cada indicador falla de forma independiente: si una fuente no responde, su valor queda a None
(se muestra como insufficient_data) y el resto del informe sigue.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Callable

import pandas as pd

from src.core.cache import FileCache
from src.core.errors import MarketDataError
from src.market.providers import cape, fred, sentiment
from src.market.service import MarketDataService
from src.models import CapeStats, MarketIndicator
from src.quant import metrics

logger = logging.getLogger(__name__)

# (id, nombre, ticker de Yahoo, unidad, tipo, nota)
_PRICE_INDICATORS: list[tuple[str, str, str, str, str, str]] = [
    ("vix", "VIX", "^VIX", "", "volatility", "Volatilidad implícita del S&P 500 a 30 días. >20 tensión, >30 pánico."),
    ("us10y", "Bono EE. UU. 10 años", "^TNX", "%", "rates", "Rentabilidad del Tesoro estadounidense a 10 años."),
    ("dxy", "Índice dólar (DXY)", "DX-Y.NYB", "", "fx", "Dólar frente a una cesta de divisas."),
    ("eurusd", "EUR/USD", "EURUSD=X", "", "fx", "Dólares por euro. Al alza, el euro se aprecia."),
]

# (id, nombre, función de `sentiment`, fuente, nota). La función se resuelve por nombre para poder sustituirla en tests.
_SENTIMENT_INDICATORS: list[tuple[str, str, str, str, str]] = [
    ("fear_greed_stocks", "Miedo y codicia · acciones", "fetch_stock_fear_greed", "CNN Fear & Greed",
     "0 = miedo extremo, 100 = codicia extrema. Combina 7 señales del mercado de acciones de EE. UU."),
    ("fear_greed_crypto", "Miedo y codicia · cripto", "fetch_crypto_fear_greed", "alternative.me",
     "0 = miedo extremo, 100 = codicia extrema. Basado en volatilidad, volumen, redes sociales y dominancia de BTC."),
]


# (id, nombre, serie FRED, transformación, unidad, tipo, nota). "yoy" = variación interanual de un índice mensual.
_FRED_INDICATORS: list[tuple[str, str, str, str, str, str, str]] = [
    ("fed_funds", "Tipo Fed (efectivo)", "DFF", "level", "%", "rates", "Tipo efectivo de los fondos federales de EE. UU."),
    ("ecb_deposit", "Tipo BCE (depósito)", "ECBDFR", "level", "%", "rates", "Facilidad de depósito del BCE: referencia de los tipos en euros."),
    ("us_curve_10y2y", "Curva EE. UU. 10a-2a", "T10Y2Y", "level", "%", "rates",
     "Diferencial 10 años menos 2 años. Negativo (curva invertida) ha precedido a recesiones."),
    ("us_real_10y", "Tipo real EE. UU. 10a", "DFII10", "level", "%", "rates",
     "Rentabilidad del bono ligado a la inflación (TIPS) a 10 años. Alto = financiación cara y presión sobre valoraciones."),
    ("us_breakeven_10y", "Inflación esperada 10a", "T10YIE", "level", "%", "inflation", "Inflación implícita a 10 años (bono nominal menos TIPS)."),
    ("us_cpi_yoy", "IPC EE. UU. (interanual)", "CPIAUCSL", "yoy", "%", "inflation", "Variación a 12 meses del IPC de EE. UU. (dato mensual)."),
    ("ez_hicp_yoy", "IPCA zona euro (interanual)", "CP0000EZ19M086NEST", "yoy", "%", "inflation",
     "Variación a 12 meses del IPCA de la zona euro (dato mensual). Objetivo del BCE: 2%."),
    ("us_unemployment", "Paro EE. UU.", "UNRATE", "level", "%", "activity", "Tasa de desempleo (dato mensual). Subidas rápidas suelen anticipar recesión."),
    ("hy_spread", "Diferencial high yield", "BAMLH0A0HYM2", "level", "%", "credit",
     "Prima de los bonos de alto rendimiento sobre el Tesoro (ICE BofA). Al alza = estrés crediticio."),
    ("nfci", "Condiciones financieras (NFCI)", "NFCI", "level", "", "credit",
     "Índice de la Fed de Chicago. Negativo = más laxas que la media histórica; positivo = más tensas."),
]
_FRED_HISTORY_YEARS = 3


def _cached(cache: FileCache, key: str, fetch: Callable[[], Any]) -> Any | None:
    """Dato de caché o recién descargado; si la descarga falla, caché caducada; si tampoco hay, None."""
    data = cache.get(key)
    if data is not None:
        return data
    try:
        data = fetch()
        cache.set(key, data)
        return data
    except MarketDataError as exc:
        data = cache.get(key, allow_stale=True)
        if data is None:
            logger.warning("%s no disponible: %s", key, exc)
        else:
            logger.warning("%s: usando caché caducada (%s)", key, exc)
        return data


def fred_values(series: pd.Series, transform: str) -> dict[str, Any]:
    """Último valor, fecha y valores de hace 1 y 12 meses de una serie FRED ya descargada."""
    values = metrics.year_over_year_pct(series) if transform == "yoy" else series
    if values.empty:
        raise MarketDataError("serie sin observaciones suficientes")
    return {
        "value": round(float(values.iloc[-1]), 4),
        "as_of": values.index[-1].strftime("%Y-%m-%d"),
        "previous": {"1m": metrics.value_months_ago(values, 1), "12m": metrics.value_months_ago(values, 12)},
    }


def _fred_indicator(cache: FileCache, spec: tuple[str, str, str, str, str, str, str]) -> MarketIndicator:
    ind_id, name, series_id, transform, unit, kind, note = spec
    start = date.today() - timedelta(days=365 * _FRED_HISTORY_YEARS)
    data = _cached(cache, f"fred_{series_id}", lambda: fred_values(fred.fetch_series(series_id, start), transform)) or {}
    return MarketIndicator(id=ind_id, name=name, value=data.get("value"), unit=unit, kind=kind, as_of=data.get("as_of"),  # type: ignore[arg-type]
                           previous=data.get("previous") or {}, source=f"FRED ({series_id})", note=note)


def cape_stats(rows: list[tuple[str, float]]) -> CapeStats:
    """Posición del CAPE actual en su historia completa y en los últimos 30 años."""
    series = pd.Series([v for _, v in rows], index=pd.to_datetime([d for d, _ in rows])).sort_index()
    current = float(series.iloc[-1])
    recent = series[series.index >= series.index[-1] - pd.DateOffset(years=30)]
    return CapeStats(
        value=round(current, 2), as_of=series.index[-1].strftime("%Y-%m-%d"), history_start=series.index[0].strftime("%Y-%m-%d"),
        previous={"1m": metrics.value_months_ago(series, 1), "12m": metrics.value_months_ago(series, 12)},
        percentile_all=metrics.percentile_rank(series, current), percentile_30y=metrics.percentile_rank(recent, current),
        median_all=round(float(series.median()), 2), median_30y=round(float(recent.median()), 2),
    )


def load_cape(cache: FileCache) -> CapeStats | None:
    rows = _cached(cache, "shiller_cape", cape.fetch_shiller_cape)
    if not rows:
        return None
    return cape_stats([(d, float(v)) for d, v in rows])


def _cape_indicator(stats: CapeStats | None) -> MarketIndicator:
    base = dict(id="cape_sp500", name="CAPE S&P 500", kind="valuation", source="multpl.com (datos de R. Shiller)")
    if stats is None:
        return MarketIndicator(**base, value=None, note="PER ajustado al ciclo (beneficios reales de 10 años).")  # type: ignore[arg-type]
    note = (f"PER ajustado al ciclo. Percentil {stats.percentile_all:.0f} desde {stats.history_start[:4]} (mediana {stats.median_all:.1f}) "
            f"y {stats.percentile_30y:.0f} en 30 años (mediana {stats.median_30y:.1f}).")
    return MarketIndicator(**base, value=stats.value, as_of=stats.as_of, previous=stats.previous, note=note)  # type: ignore[arg-type]


def _price_indicator(service: MarketDataService, spec: tuple[str, str, str, str, str, str]) -> MarketIndicator:
    ind_id, name, ticker, unit, kind, note = spec
    try:
        close = service.get_close(ticker).close
    except MarketDataError as exc:
        logger.warning("Indicador %s no disponible: %s", ind_id, exc)
        return MarketIndicator(id=ind_id, name=name, value=None, unit=unit, kind=kind, source=f"Yahoo Finance ({ticker})", note=note)  # type: ignore[arg-type]
    last = float(close.iloc[-1])

    def back(months: int) -> float | None:
        ret = metrics.period_return(close, months)
        return round(last / (1 + ret / 100), 4) if ret is not None else None

    return MarketIndicator(
        id=ind_id, name=name, value=round(last, 4), unit=unit, kind=kind,  # type: ignore[arg-type]
        as_of=close.index[-1].strftime("%Y-%m-%d"), source=f"Yahoo Finance ({ticker})", note=note,
        previous={"1m": back(1), "12m": back(12)},
    )


def _sentiment_indicator(cache: FileCache, spec: tuple[str, str, str, str, str]) -> MarketIndicator:
    ind_id, name, fetch_name, source, note = spec
    data = _cached(cache, ind_id, lambda: getattr(sentiment, fetch_name)())
    if data is None:
        return MarketIndicator(id=ind_id, name=name, value=None, kind="sentiment", source=source, note=note)
    return MarketIndicator(
        id=ind_id, name=name, value=data.get("value"), kind="sentiment", as_of=data.get("as_of"),
        rating=data.get("rating"), previous=data.get("previous") or {}, source=source, note=note,
    )


def build_indicators(service: MarketDataService, sentiment_cache: FileCache,
                     macro_cache: FileCache | None = None, cape_data: CapeStats | None = None) -> list[MarketIndicator]:
    """`macro_cache` None desactiva FRED y CAPE (`data.macro_data: false`)."""
    indicators = [_sentiment_indicator(sentiment_cache, spec) for spec in _SENTIMENT_INDICATORS]
    indicators += [_price_indicator(service, spec) for spec in _PRICE_INDICATORS]
    if macro_cache is not None:
        indicators += [_fred_indicator(macro_cache, spec) for spec in _FRED_INDICATORS]
        indicators.append(_cape_indicator(cape_data))
    return indicators
