"""Acceso a datos de mercado con caché local y conversión de divisa.

Esta capa solo obtiene datos crudos; los cálculos están en `src.quant`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from src.core.cache import FileCache
from src.core.errors import MarketDataError
from src.market.providers.base import FundamentalsProvider, PriceProvider
from src.models import ValuationMetrics

logger = logging.getLogger(__name__)


@dataclass
class PriceResult:
    close: pd.Series
    from_stale_cache: bool = False


@dataclass
class OhlcResult:
    ohlc: pd.DataFrame
    from_stale_cache: bool = False


def _series_to_json(series: pd.Series) -> dict[str, float]:
    return {idx.strftime("%Y-%m-%d"): float(v) for idx, v in series.items()}


def _series_from_json(data: dict[str, float], name: str) -> pd.Series:
    series = pd.Series(data, dtype=float)
    series.index = pd.to_datetime(series.index)
    series.name = name
    return series.sort_index()


def _frame_to_json(df: pd.DataFrame) -> dict[str, list]:
    return {"index": [idx.strftime("%Y-%m-%d") for idx in df.index], **{c: [float(v) for v in df[c]] for c in df.columns}}


def _frame_from_json(data: dict[str, list]) -> pd.DataFrame:
    index = pd.to_datetime(data["index"])
    return pd.DataFrame({k: v for k, v in data.items() if k != "index"}, index=index, dtype=float).sort_index()


class MarketDataService:
    """Fachada sobre los proveedores: caché, fallback a caché caducada y FX."""

    def __init__(
        self,
        price_provider: PriceProvider,
        fundamentals_provider: FundamentalsProvider,
        price_cache: FileCache,
        fundamentals_cache: FileCache,
        period: str,
    ) -> None:
        self.prices = price_provider
        self.fundamentals = fundamentals_provider
        self.price_cache = price_cache
        self.fundamentals_cache = fundamentals_cache
        self.period = period
        self._memo: dict[str, PriceResult] = {}

    def get_close(self, ticker: str) -> PriceResult:
        """Cierres de `ticker`. Si el proveedor falla y hay caché caducada, la usa y lo marca."""
        if ticker in self._memo:
            return self._memo[ticker]
        key = f"prices:{self.prices.name}:{ticker}:{self.period}"
        cached = self.price_cache.get(key)
        if cached:
            logger.debug("Precios de %s desde caché", ticker)
            result = PriceResult(_series_from_json(cached, ticker))
        else:
            try:
                close = self.prices.get_close_history(ticker, self.period)
                self.price_cache.set(key, _series_to_json(close))
                result = PriceResult(close)
                logger.info("Precios de %s descargados (%d observaciones)", ticker, len(close))
            except MarketDataError:
                stale = self.price_cache.get(key, allow_stale=True)
                if not stale:
                    raise
                logger.warning("Proveedor no disponible para %s: usando caché caducada", ticker)
                result = PriceResult(_series_from_json(stale, ticker), from_stale_cache=True)
        self._memo[ticker] = result
        return result

    def get_ohlc(self, ticker: str, period: str | None = None) -> OhlcResult:
        """Velas diarias de `ticker`, con la misma caché y fallback a caché caducada que `get_close`."""
        period = period or self.period
        if not hasattr(self.prices, "get_ohlc_history"):
            raise MarketDataError(f"El proveedor {self.prices.name} no ofrece OHLC")
        key = f"ohlc:{self.prices.name}:{ticker}:{period}"
        cached = self.price_cache.get(key)
        if cached:
            logger.debug("OHLC de %s desde caché", ticker)
            return OhlcResult(_frame_from_json(cached))
        try:
            ohlc = self.prices.get_ohlc_history(ticker, period)
        except MarketDataError:
            stale = self.price_cache.get(key, allow_stale=True)
            if not stale:
                raise
            logger.warning("Proveedor no disponible para %s: usando OHLC de caché caducada", ticker)
            return OhlcResult(_frame_from_json(stale), from_stale_cache=True)
        self.price_cache.set(key, _frame_to_json(ohlc))
        logger.info("OHLC de %s descargado (%d sesiones)", ticker, len(ohlc))
        return OhlcResult(ohlc)

    def get_close_in_currency(self, ticker: str, ticker_ccy: str, target_ccy: str) -> PriceResult:
        """Cierres convertidos a `target_ccy` usando el cruce de Yahoo `{target}{ticker_ccy}=X`."""
        base = self.get_close(ticker)
        if ticker_ccy == target_ccy:
            return base
        fx = self.get_close(f"{target_ccy}{ticker_ccy}=X")  # p. ej. EURUSD=X = USD por 1 EUR
        aligned_fx = fx.close.reindex(base.close.index, method="ffill")
        converted = (base.close / aligned_fx).dropna()
        converted.name = f"{ticker}@{target_ccy}"
        return PriceResult(converted, from_stale_cache=base.from_stale_cache or fx.from_stale_cache)

    def get_valuation(self, ticker: str) -> ValuationMetrics:
        key = f"valuation:{self.fundamentals.name}:{ticker}"
        cached = self.fundamentals_cache.get(key)
        if cached:
            return ValuationMetrics.model_validate(cached)
        metrics = self.fundamentals.get_valuation(ticker)
        self.fundamentals_cache.set(key, metrics.model_dump())
        return metrics
