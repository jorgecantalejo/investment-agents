"""Interfaz de proveedores de búsqueda web (Tavily hoy; otros en el futuro)."""

from __future__ import annotations

from typing import Protocol, TypedDict


class RawResult(TypedDict):
    title: str
    url: str
    content: str
    published_date: str | None


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, *, max_results: int, time_range: str, depth: str, include_domains: list[str] | None = None) -> list[RawResult]:
        """`include_domains` limita los resultados a esas webs. Lanza SearchQuotaExceededError, SearchAuthError o SearchError."""
        ...
