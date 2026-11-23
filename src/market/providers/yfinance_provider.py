"""Proveedor basado en yfinance (Yahoo Finance, gratuito y sin clave)."""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd
import requests
import yfinance as yf

from src.core.errors import InvalidTickerError, NetworkError
from src.core.retry import retry_call
from src.models import ValuationMetrics

logger = logging.getLogger(__name__)

_TRANSIENT: tuple[type[BaseException], ...] = (
    requests.exceptions.RequestException,
    ConnectionError,
    TimeoutError,
    yf.exceptions.YFRateLimitError,
)


def _positive(value: object) -> float | None:
    """Devuelve float positivo y finito, o None (yfinance a veces da 0, 'Infinity' o NaN)."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")) or number <= 0:
        return None
    return number


class YFinanceProvider:
    """Precios y fundamentales básicos desde Yahoo Finance."""

    name = "yfinance"

    def _history(self, ticker: str, period: str) -> pd.DataFrame:
        def fetch() -> pd.DataFrame:
            return yf.Ticker(ticker).history(period=period, auto_adjust=True)

        try:
            df = retry_call(fetch, retry_on=_TRANSIENT, attempts=3, label=f"yfinance {ticker}")
        except _TRANSIENT as exc:
            raise NetworkError(f"Fallo de red obteniendo {ticker}: {exc}") from exc
        except Exception as exc:  # yfinance lanza tipos variados según versión
            raise NetworkError(f"Error del proveedor para {ticker}: {exc}") from exc

        if df is None or df.empty or "Close" not in df:
            raise InvalidTickerError(f"Sin datos para '{ticker}' (ticker inválido, deslistado o proveedor caído)")
        index = pd.DatetimeIndex(df.index)
        if index.tz is not None:
            index = index.tz_localize(None)
        df = df.copy()
        df.index = index.normalize()
        return df[~df.index.duplicated(keep="last")].sort_index()

    def get_close_history(self, ticker: str, period: str) -> pd.Series:
        close = self._history(ticker, period)["Close"].dropna()
        close.name = ticker
        return close.astype(float)

    def get_ohlc_history(self, ticker: str, period: str) -> pd.DataFrame:
        """Velas diarias (open, high, low, close) sin filas incompletas."""
        df = self._history(ticker, period)
        missing = [c for c in ("Open", "High", "Low", "Close") if c not in df]
        if missing:
            raise InvalidTickerError(f"'{ticker}' no tiene columnas {missing} en el proveedor")
        ohlc = df[["Open", "High", "Low", "Close"]].dropna().astype(float)
        ohlc.columns = ["open", "high", "low", "close"]
        return ohlc

    def get_valuation(self, ticker: str) -> ValuationMetrics:
        try:
            info = retry_call(lambda: yf.Ticker(ticker).info or {}, retry_on=_TRANSIENT, attempts=2, label=f"yfinance info {ticker}")
        except Exception as exc:
            logger.warning("No se pudieron obtener fundamentales de %s: %s", ticker, exc)
            info = {}
        # 'yield' (ETFs) y 'trailingAnnualDividendYield' vienen como fracción; 'dividendYield'
        # cambia de formato entre versiones de yfinance, por eso no se usa.
        dividend = _positive(info.get("yield")) or _positive(info.get("trailingAnnualDividendYield"))
        return ValuationMetrics(
            source_ticker=ticker,
            provider=self.name,
            as_of=date.today().isoformat(),
            trailing_pe=_positive(info.get("trailingPE")),
            forward_pe=_positive(info.get("forwardPE")),
            cape=None,  # yfinance no ofrece CAPE: requiere otro proveedor
            ev_ebitda=_positive(info.get("enterpriseToEbitda")),
            price_to_sales=_positive(info.get("priceToSalesTrailing12Months")),
            dividend_yield_pct=round(dividend * 100, 2) if dividend else None,
        )
