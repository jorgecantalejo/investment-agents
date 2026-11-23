"""Rutas del proyecto y secretos leídos de variables de entorno (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    """Valores de entorno. Las claves nunca se escriben en logs ni en el JSON de auditoría."""

    openai_api_key: str | None
    tavily_api_key: str | None
    openai_model: str | None
    anthropic_api_key: str | None = None
    project_root: Path = PROJECT_ROOT

    @property
    def data_dir(self) -> Path:
        return self.project_root / "data"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def logs_dir(self) -> Path:
        return self.project_root / "logs"

    @property
    def templates_dir(self) -> Path:
        return self.project_root / "templates"


def load_settings() -> Settings:
    """Carga `.env` (si existe) sin sobrescribir variables ya definidas en el entorno."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    return Settings(
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        tavily_api_key=os.getenv("TAVILY_API_KEY") or None,
        openai_model=os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or None,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
    )
