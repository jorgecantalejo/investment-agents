"""Interfaces de proveedores de datos de mercado.

Para añadir una fuente nueva (p. ej. un proveedor de CAPE, FRED, Stooq...)
basta con implementar uno de estos protocolos y registrarlo en `MarketDataService`.
"""

from __future__ import annotations

from typing import Protocol

import pandas as pd

from src.models import ValuationMetrics


class PriceProvider(Protocol):
    name: str

    def get_close_history(self, ticker: str, period: str) -> pd.Series:
        """Serie de cierres ajustados con índice de fechas sin zona horaria. Lanza MarketDataError."""
        ...


class OhlcProvider(Protocol):
    name: str

    def get_ohlc_history(self, ticker: str, period: str) -> pd.DataFrame:
        """Velas diarias con columnas open/high/low/close e índice de fechas sin zona horaria. Lanza MarketDataError."""
        ...


class FundamentalsProvider(Protocol):
    name: str

    def get_valuation(self, ticker: str) -> ValuationMetrics:
        """Métricas de valuación disponibles; los campos sin dato quedan a None."""
        ...
