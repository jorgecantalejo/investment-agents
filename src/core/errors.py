"""Jerarquía de excepciones del proyecto.

Cada capa lanza errores propios para que el pipeline pueda degradar de forma
controlada (seguir sin web, sin LLM, sin un activo...) en lugar de abortar.
"""


class InvestmentAgentsError(Exception):
    """Error base del proyecto."""


class ConfigError(InvestmentAgentsError):
    """Configuración YAML inválida o inexistente."""


class MarketDataError(InvestmentAgentsError):
    """Error genérico al obtener datos de mercado."""


class InvalidTickerError(MarketDataError):
    """El proveedor no devuelve datos para el ticker (inválido, deslistado o sin cobertura)."""


class NetworkError(MarketDataError):
    """Fallo de red o del proveedor tras agotar reintentos."""


class InsufficientDataError(InvestmentAgentsError):
    """Hay datos, pero no los suficientes para un cálculo concreto."""


class SearchError(InvestmentAgentsError):
    """Fallo en la búsqueda web."""


class SearchQuotaExceededError(SearchError):
    """Se agotaron los créditos del proveedor o el presupuesto de búsquedas de la ejecución."""


class SearchAuthError(SearchError):
    """Clave de API de búsqueda ausente o inválida."""


class LLMError(InvestmentAgentsError):
    """Fallo al ejecutar un agente LLM."""


class LLMUnavailableError(LLMError):
    """El LLM no está disponible (sin clave, clave inválida...). No tiene sentido reintentar."""


class ReportError(InvestmentAgentsError):
    """Fallo al generar el informe HTML."""


class LLMPendingError(LLMError):
    """Modo handoff: la respuesta del agente aún no se ha escrito en disco."""
