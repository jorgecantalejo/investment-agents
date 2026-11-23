"""Caché local en disco con caducidad (TTL).

Se usa para precios, fundamentales y búsquedas web, evitando llamadas
repetidas a APIs con cuota. Los ficheros son JSON legibles para poder auditarlos.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class FileCache:
    """Caché clave→JSON con TTL en horas. `enabled=False` la convierte en no-op de lectura."""

    def __init__(self, directory: Path, ttl_hours: float, enabled: bool = True) -> None:
        self.directory = directory
        self.ttl_seconds = ttl_hours * 3600
        self.enabled = enabled
        directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        return self.directory / f"{digest}.json"

    def get(self, key: str, *, allow_stale: bool = False) -> Any | None:
        """Devuelve el valor cacheado o None si no existe o ha caducado."""
        if not self.enabled and not allow_stale:
            return None
        path = self._path(key)
        if not path.exists():
            return None
        age = time.time() - path.stat().st_mtime
        if age > self.ttl_seconds and not allow_stale:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Entrada de caché corrupta %s: %s", path.name, exc)
            return None
        return payload.get("value")

    def set(self, key: str, value: Any) -> None:
        """Guarda un valor serializable a JSON. Los fallos de escritura no son fatales."""
        try:
            self._path(key).write_text(
                json.dumps({"key": key, "saved_at": time.time(), "value": value}, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("No se pudo escribir la caché para %s: %s", key, exc)
