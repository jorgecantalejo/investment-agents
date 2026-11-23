"""Configuración de logging: consola + fichero por ejecución con el run_id en cada línea."""

from __future__ import annotations

import logging
from pathlib import Path


class _RunIdFilter(logging.Filter):
    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        return True


def setup_logging(run_id: str, log_dir: Path, level: str = "INFO") -> Path:
    """Configura el logger raíz y devuelve la ruta del fichero de log de esta ejecución."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"run_{run_id}.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(run_id)s] %(name)s: %(message)s")
    run_filter = _RunIdFilter(run_id)

    console = logging.StreamHandler()
    console.setLevel(level.upper())
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)
    for handler in (console, file_handler):
        handler.setFormatter(fmt)
        handler.addFilter(run_filter)
        root.addHandler(handler)

    # Librerías ruidosas: solo avisos.
    for noisy in ("httpx", "httpcore", "urllib3", "yfinance", "peewee", "openai", "openai.agents"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_file
