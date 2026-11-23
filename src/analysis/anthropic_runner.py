"""Ejecución de los agentes con Claude (SDK oficial de Anthropic) y salida estructurada Pydantic.

Mismo contrato que `AgentRunner` (OpenAI): `run(instrucciones, prompt, tipo, etiqueta)`.
Los agentes no usan herramientas, así que cada agente es una única llamada a la Messages API.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from src.analysis import prompts
from src.core.errors import LLMError, LLMUnavailableError
from src.core.retry import retry_async

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "claude-opus-5-5"
# Reintenta en otro modelo si los clasificadores de seguridad rechazan la petición (falsos positivos).
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class _BadOutput(Exception):
    """Respuesta truncada o que no valida contra el esquema: merece un reintento."""


def build_instructions() -> dict[str, str]:
    """Instrucciones de sistema por rol (equivalente a `build_agents` en el backend OpenAI)."""
    return {
        "macro": prompts.MACRO_INSTRUCTIONS,
        "asset": prompts.ASSET_INSTRUCTIONS,
        "supervisor": prompts.SUPERVISOR_INSTRUCTIONS,
    }


class AnthropicRunner:
    def __init__(self, *, model: str | None, effort: str, max_retries: int, timeout_seconds: float, api_key: str | None) -> None:
        self.model = model or DEFAULT_MODEL
        self.effort = effort
        self.max_retries = max_retries
        # Sin api_key explícita el SDK usa ANTHROPIC_API_KEY o el perfil de `ant auth login`.
        kwargs = {"api_key": api_key} if api_key else {}
        self.client = anthropic.AsyncAnthropic(max_retries=max_retries, timeout=timeout_seconds, **kwargs)

    async def run(self, instructions: str, prompt: str, output_type: type[T], label: str) -> T:
        """Devuelve la salida validada. Lanza LLMUnavailableError (no reintentar) o LLMError."""

        async def once() -> T:
            try:
                response = await self.client.beta.messages.parse(
                    model=self.model,
                    max_tokens=16000,
                    system=instructions,
                    messages=[{"role": "user", "content": prompt}],
                    output_format=output_type,
                    output_config={"effort": self.effort},
                    betas=[_FALLBACK_BETA],
                    fallbacks="default",
                )
            except ValidationError as exc:
                raise _BadOutput(f"JSON no válido para {output_type.__name__}: {exc}") from exc
            if response.stop_reason == "refusal":
                category = response.stop_details.category if response.stop_details else None
                raise LLMError(f"Claude rechazó la petición del agente {label} (categoría: {category})")
            if response.stop_reason == "max_tokens" or response.parsed_output is None:
                raise _BadOutput(f"Respuesta incompleta (stop_reason={response.stop_reason})")
            usage = response.usage
            logger.debug("Agente %s: %s tokens entrada, %s salida (modelo %s)", label, usage.input_tokens, usage.output_tokens, response.model)
            return response.parsed_output

        logger.info("Agente %s: inicio (%s, effort=%s)", label, self.model, self.effort)
        try:
            # El SDK ya reintenta 429/5xx/red; aquí solo se reintenta una salida inválida.
            output = await retry_async(once, retry_on=(_BadOutput,), attempts=self.max_retries + 1, label=f"agente {label}")
        except TypeError as exc:
            # El SDK lanza TypeError al construir la petición si no encuentra ninguna credencial.
            if "authentication" not in str(exc).lower():
                raise
            raise LLMUnavailableError("No hay credenciales de Anthropic: define ANTHROPIC_API_KEY en .env") from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise LLMUnavailableError(f"Anthropic rechaza la clave: {exc.message}") from exc
        except anthropic.NotFoundError as exc:
            raise LLMUnavailableError(f"Modelo '{self.model}' no disponible: {exc.message}") from exc
        except anthropic.BadRequestError as exc:
            msg = exc.message
            if "credit balance" in msg.lower():
                raise LLMUnavailableError(f"Sin saldo en la cuenta de Anthropic: {msg}") from exc
            raise LLMError(f"Petición rechazada para el agente {label}: {msg}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError(f"Límite de uso de Anthropic alcanzado en el agente {label}: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Error {exc.status_code} de Anthropic en el agente {label}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Fallo de red con Anthropic en el agente {label}: {exc}") from exc
        except (_BadOutput, asyncio.TimeoutError) as exc:
            raise LLMError(f"Agente {label} falló: {exc}") from exc
        logger.info("Agente %s: completado", label)
        return output
