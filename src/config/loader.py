"""Carga y validación del fichero YAML de configuración."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from src.config.models import AppConfig
from src.core.errors import ConfigError


def load_config(path: Path) -> AppConfig:
    """Lee `path` y devuelve un `AppConfig` validado o lanza `ConfigError` con un mensaje legible."""
    if not path.exists():
        raise ConfigError(f"No existe el fichero de configuración: {path}. Copia config.example.yaml como punto de partida.")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML mal formado en {path}: {exc}") from exc
    return parse_config(raw, source=str(path))


def parse_config(raw: object, source: str = "<dict>") -> AppConfig:
    """Valida un diccionario ya cargado (útil en tests)."""
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: la raíz del YAML debe ser un mapa")
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        lines = [f"  - {'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()]
        raise ConfigError(f"Configuración inválida en {source}:\n" + "\n".join(lines)) from exc
