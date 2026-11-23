"""Reintentos con backoff exponencial para llamadas síncronas y asíncronas."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")
logger = logging.getLogger(__name__)


def retry_call(
    func: Callable[[], T],
    *,
    retry_on: tuple[type[BaseException], ...],
    attempts: int = 3,
    base_delay: float = 1.0,
    label: str = "",
) -> T:
    """Ejecuta `func` reintentando ante las excepciones indicadas."""
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except retry_on as exc:
            if attempt == attempts:
                raise
            delay = base_delay * 2 ** (attempt - 1)
            logger.warning("%s: intento %d/%d fallido (%s). Reintento en %.1fs", label, attempt, attempts, exc, delay)
            time.sleep(delay)
    raise RuntimeError("unreachable")


async def retry_async(
    func: Callable[[], Awaitable[T]],
    *,
    retry_on: tuple[type[BaseException], ...],
    attempts: int = 3,
    base_delay: float = 2.0,
    label: str = "",
) -> T:
    """Versión asíncrona de `retry_call`."""
    for attempt in range(1, attempts + 1):
        try:
            return await func()
        except retry_on as exc:
            if attempt == attempts:
                raise
            delay = base_delay * 2 ** (attempt - 1)
            logger.warning("%s: intento %d/%d fallido (%s). Reintento en %.1fs", label, attempt, attempts, exc, delay)
            await asyncio.sleep(delay)
    raise RuntimeError("unreachable")
