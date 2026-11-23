"""Búsqueda web controlada: presupuesto por ejecución, caché, circuit breaker y registro de fuentes.

La búsqueda la hace Python (no el LLM) para que cada fuente quede registrada con
id, URL y fecha, y para limitar el gasto de créditos de forma determinista.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.config.models import ResearchConfig
from src.core.cache import FileCache
from src.core.errors import SearchAuthError, SearchError, SearchQuotaExceededError
from src.models import SearchUsage, Source
from src.research.providers.base import RawResult, SearchProvider

logger = logging.getLogger(__name__)


class ResearchService:
    def __init__(self, provider: SearchProvider | None, config: ResearchConfig, cache: FileCache, disabled_reason: str | None = None) -> None:
        self.provider = provider
        self.config = config
        self.cache = cache
        self.sources: list[Source] = []
        self._by_url: dict[str, Source] = {}
        self.usage = SearchUsage(budget=config.max_searches_per_run, disabled_reason=disabled_reason)
        if provider is None and not disabled_reason:
            self.usage.disabled_reason = "búsqueda web desactivada"

    @property
    def available(self) -> bool:
        return self.provider is not None and not self.usage.quota_exhausted and self.usage.disabled_reason is None

    def _register(self, raw: RawResult, query: str, scope: str) -> Source:
        url = raw["url"]
        if url in self._by_url:
            return self._by_url[url]
        source = Source(
            id=f"S{len(self.sources) + 1}",
            title=raw["title"].strip(),
            url=url,
            published_date=(raw.get("published_date") or None),
            retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            query=query,
            scope=scope,
            snippet=" ".join(raw["content"].split())[: self.config.snippet_chars],
            provider=self.provider.name if self.provider else "cache",
        )
        self.sources.append(source)
        self._by_url[url] = source
        return source

    def search(self, query: str, *, scope: str, time_range: str = "week", domains: list[str] | None = None) -> list[Source]:
        """Devuelve las fuentes de una consulta (limitada a `domains` si se dan). Nunca lanza: los fallos se reflejan en `usage`."""
        key = f"{query}|{time_range}|{self.config.results_per_query}|{self.config.search_depth}"
        if domains:
            key += "|" + ",".join(sorted(domains))
        cached = self.cache.get(key)
        if cached is not None:
            self.usage.cache_hits += 1
            return [self._register(r, query, scope) for r in cached]

        if not self.available:
            return []
        if self.usage.performed >= self.usage.budget:
            logger.warning("Presupuesto de búsquedas agotado (%d); se omite: %s", self.usage.budget, query)
            self.usage.quota_exhausted = True
            return []

        assert self.provider is not None
        self.usage.performed += 1
        try:
            results = self.provider.search(
                query, max_results=self.config.results_per_query, time_range=time_range, depth=self.config.search_depth,
                include_domains=domains or None,
            )
        except SearchQuotaExceededError as exc:
            logger.error("%s. No se harán más búsquedas en esta ejecución.", exc)
            self.usage.quota_exhausted = True
            self.usage.failed += 1
            return []
        except SearchAuthError as exc:
            logger.error("%s. Búsqueda web desactivada.", exc)
            self.usage.disabled_reason = str(exc)
            self.usage.failed += 1
            return []
        except SearchError as exc:
            logger.warning("Búsqueda fallida '%s': %s", query, exc)
            self.usage.failed += 1
            return []

        self.cache.set(key, results)
        logger.info("Búsqueda '%s'%s: %d resultados", query, f" en {len(domains)} webs preferidas" if domains else "", len(results))
        return [self._register(r, query, scope) for r in results]
