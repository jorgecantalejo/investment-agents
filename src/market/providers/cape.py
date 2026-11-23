"""CAPE (Shiller PE) del S&P 500: serie mensual desde 1871 con el dato más reciente estimado a precio actual.

Se lee de la tabla pública de multpl.com (basada en los datos de Robert Shiller). Es una fuente no oficial:
si cambia su formato, el CAPE aparece como insufficient_data y el resto del informe no se ve afectado.
"""

from __future__ import annotations

import re
from datetime import datetime

import requests

from src.core.errors import NetworkError
from src.core.retry import retry_call

_URL = "https://www.multpl.com/shiller-pe/table/by-month"
_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}
_ROW = re.compile(r"<td>\s*([A-Z][a-z]{2} \d{1,2}, \d{4})\s*</td>\s*<td>(.*?)</td>", re.S)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_TRANSIENT: tuple[type[BaseException], ...] = (requests.exceptions.RequestException,)


def parse_table(html: str) -> list[tuple[str, float]]:
    """Filas (fecha ISO, CAPE) en orden cronológico."""
    rows = []
    for raw_date, cell in _ROW.findall(html):
        number = _NUMBER.search(re.sub(r"&#?\w+;", " ", cell))
        if number:
            rows.append((datetime.strptime(raw_date, "%b %d, %Y").date().isoformat(), float(number.group())))
    if len(rows) < 120:  # menos de 10 años: el formato ha cambiado
        raise NetworkError(f"Tabla de CAPE no reconocida ({len(rows)} filas)")
    return sorted(rows)


def fetch_shiller_cape() -> list[tuple[str, float]]:
    def call() -> str:
        response = requests.get(_URL, headers=_HEADERS, timeout=30)
        response.raise_for_status()
        return response.text

    try:
        return parse_table(retry_call(call, retry_on=_TRANSIENT, attempts=2, label=_URL))
    except requests.exceptions.RequestException as exc:
        raise NetworkError(f"No se pudo leer el CAPE de {_URL}: {exc}") from exc
