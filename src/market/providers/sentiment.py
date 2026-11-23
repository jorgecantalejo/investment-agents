"""Índices de miedo y codicia (acciones y cripto) desde fuentes públicas sin clave.

- Acciones: CNN Fear & Greed (endpoint de datos no oficial de su web; puede cambiar sin aviso).
- Cripto: alternative.me Crypto Fear & Greed (API pública documentada).
"""

from __future__ import annotations

from datetime import datetime, timezone

import requests

from src.core.errors import NetworkError
from src.core.retry import retry_call

_CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
_CRYPTO_URL = "https://api.alternative.me/fng/?limit=31"
_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}
_TRANSIENT: tuple[type[BaseException], ...] = (requests.exceptions.RequestException,)


def _get_json(url: str) -> dict:
    def call() -> dict:
        response = requests.get(url, headers=_HEADERS, timeout=15)
        response.raise_for_status()
        return response.json()

    try:
        return retry_call(call, retry_on=_TRANSIENT, attempts=2, label=url)
    except (requests.exceptions.RequestException, ValueError) as exc:
        raise NetworkError(f"No se pudo leer {url}: {exc}") from exc


def _number(value: object) -> float | None:
    try:
        return round(float(value), 1)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def fetch_stock_fear_greed() -> dict:
    """Devuelve {value, rating, as_of, previous: {1d, 1w, 1m, 1y}}."""
    data = _get_json(_CNN_URL).get("fear_and_greed") or {}
    return {
        "value": _number(data.get("score")),
        "rating": data.get("rating"),
        "as_of": (data.get("timestamp") or "")[:10] or None,
        "previous": {
            "1d": _number(data.get("previous_close")),
            "1w": _number(data.get("previous_1_week")),
            "1m": _number(data.get("previous_1_month")),
            "1y": _number(data.get("previous_1_year")),
        },
    }


def fetch_crypto_fear_greed() -> dict:
    """Devuelve el mismo formato que `fetch_stock_fear_greed` (sin dato a 1 año)."""
    rows = _get_json(_CRYPTO_URL).get("data") or []
    if not rows:
        raise NetworkError("alternative.me no devolvió datos")

    def at(index: int) -> float | None:
        return _number(rows[index]["value"]) if len(rows) > index else None

    timestamp = rows[0].get("timestamp")
    as_of = datetime.fromtimestamp(int(timestamp), tz=timezone.utc).date().isoformat() if timestamp else None
    return {
        "value": at(0),
        "rating": rows[0].get("value_classification"),
        "as_of": as_of,
        "previous": {"1d": at(1), "1w": at(7), "1m": at(30), "1y": None},
    }
