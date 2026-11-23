"""Proveedor de búsqueda Tavily con traducción de errores a la jerarquía del proyecto."""

from __future__ import annotations

import requests
from tavily import TavilyClient
from tavily import errors as tavily_errors

from src.core.errors import SearchAuthError, SearchError, SearchQuotaExceededError
from src.core.retry import retry_call
from src.research.providers.base import RawResult

_TRANSIENT: tuple[type[BaseException], ...] = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    tavily_errors.TimeoutError,
)


class TavilyProvider:
    name = "tavily"

    def __init__(self, api_key: str) -> None:
        self._client = TavilyClient(api_key=api_key)

    def search(self, query: str, *, max_results: int, time_range: str, depth: str, include_domains: list[str] | None = None) -> list[RawResult]:
        def call() -> dict:
            return self._client.search(
                query=query,
                search_depth=depth,
                max_results=max_results,
                topic="finance",
                time_range=time_range,
                include_domains=include_domains or None,
            )

        try:
            response = retry_call(call, retry_on=_TRANSIENT, attempts=3, label=f"tavily '{query[:40]}'")
        except tavily_errors.UsageLimitExceededError as exc:
            raise SearchQuotaExceededError(f"Créditos de Tavily agotados: {exc}") from exc
        except (tavily_errors.InvalidAPIKeyError, tavily_errors.MissingAPIKeyError, tavily_errors.ForbiddenError) as exc:
            raise SearchAuthError(f"Clave de Tavily inválida o sin permisos: {exc}") from exc
        except Exception as exc:
            raise SearchError(f"Fallo en Tavily: {exc}") from exc

        return [
            RawResult(
                title=item.get("title") or "(sin título)",
                url=item.get("url") or "",
                content=item.get("content") or "",
                published_date=item.get("published_date"),
            )
            for item in response.get("results", [])
            if item.get("url")
        ]
