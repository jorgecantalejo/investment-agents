"""Series macro de FRED (Reserva Federal de St. Louis) desde su descarga CSV pública, sin clave de API."""

from __future__ import annotations

import io
from datetime import date

import pandas as pd
import requests

from src.core.errors import NetworkError
from src.core.retry import retry_call

_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
_TRANSIENT: tuple[type[BaseException], ...] = (requests.exceptions.RequestException,)


def parse_csv(text: str, series_id: str) -> pd.Series:
    """CSV de FRED (`observation_date,<id>`) -> serie float indexada por fecha. Los huecos ('.') se descartan."""
    frame = pd.read_csv(io.StringIO(text), parse_dates=[0], index_col=0, na_values=["."])
    if series_id not in frame.columns:
        raise NetworkError(f"FRED no devolvió la serie {series_id}")
    series = pd.to_numeric(frame[series_id], errors="coerce").dropna()
    if series.empty:
        raise NetworkError(f"FRED devolvió la serie {series_id} vacía")
    return series


def fetch_series(series_id: str, start: date) -> pd.Series:
    def call() -> str:
        response = requests.get(_CSV_URL, params={"id": series_id, "cosd": start.isoformat()}, timeout=30)
        response.raise_for_status()
        return response.text

    try:
        text = retry_call(call, retry_on=_TRANSIENT, attempts=2, label=f"FRED {series_id}")
    except requests.exceptions.RequestException as exc:
        raise NetworkError(f"No se pudo leer la serie {series_id} de FRED: {exc}") from exc
    return parse_csv(text, series_id)
