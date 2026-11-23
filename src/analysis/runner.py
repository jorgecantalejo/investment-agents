"""Ejecución de agentes con timeout, reintentos y traducción de errores."""

from __future__ import annotations

import asyncio
import logging
from typing import TypeVar

import openai
from agents import Agent, AgentsException, Runner, set_tracing_disabled
from pydantic import BaseModel

from src.core.errors import LLMError, LLMUnavailableError
from src.core.retry import retry_async

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

_TRANSIENT: tuple[type[BaseException], ...] = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.RateLimitError,
    openai.InternalServerError,
    asyncio.TimeoutError,
    AgentsException,  # incluye ModelBehaviorError (JSON inválido) -> merece un reintento
)


class AgentRunner:
    def __init__(self, *, max_retries: int, timeout_seconds: float, tracing: bool) -> None:
        self.max_retries = max_retries
        self.timeout = timeout_seconds
        set_tracing_disabled(not tracing)

    async def run(self, agent: Agent, prompt: str, output_type: type[T], label: str) -> T:
        """Ejecuta el agente y devuelve su salida validada. Lanza LLMUnavailableError o LLMError."""

        async def once() -> T:
            result = await asyncio.wait_for(Runner.run(agent, prompt, max_turns=3), timeout=self.timeout)
            output = result.final_output
            if not isinstance(output, output_type):
                output = output_type.model_validate(output)
            return output

        logger.info("Agente %s: inicio", label)
        try:
            output = await retry_async(once, retry_on=_TRANSIENT, attempts=self.max_retries + 1, label=f"agente {label}")
        except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
            raise LLMUnavailableError(f"OpenAI rechaza la clave: {exc}") from exc
        except openai.NotFoundError as exc:
            raise LLMUnavailableError(f"Modelo no disponible: {exc}") from exc
        except (openai.OpenAIError, AgentsException, asyncio.TimeoutError, ValueError) as exc:
            raise LLMError(f"Agente {label} falló: {type(exc).__name__}: {exc}") from exc
        logger.info("Agente %s: completado", label)
        return output
