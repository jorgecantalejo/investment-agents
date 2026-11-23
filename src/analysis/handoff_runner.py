"""Runner «handoff»: el LLM es externo (p. ej. Claude Code) y se comunica por ficheros.

Por cada agente se escribe `requests/<etiqueta>.md` (instrucciones + entrada + esquema JSON).
Quien haga de analista escribe `responses/<etiqueta>.json`; en la siguiente ejecución se valida
contra el mismo esquema Pydantic y el pipeline continúa como con cualquier otro proveedor.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from src.analysis.schemas import AssetAnalysis, MacroAnalysis, SupervisorOutput
from src.core.errors import LLMPendingError

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

_REQUEST_TEMPLATE = """# Petición para el agente «{label}»

Responde escribiendo **solo JSON válido** que cumpla el esquema del final en:

    {response_path}

## Instrucciones del agente

{instructions}

## Entrada

{prompt}

## Esquema JSON de la respuesta ({model})

```json
{schema}
```
"""


class HandoffRunner:
    def __init__(self, directory: Path) -> None:
        self.requests = directory / "requests"
        self.responses = directory / "responses"
        self.requests.mkdir(parents=True, exist_ok=True)
        self.responses.mkdir(parents=True, exist_ok=True)

    async def run(self, instructions: str, prompt: str, output_type: type[T], label: str) -> T:
        response_path = self.responses / f"{label}.json"
        error_path = self.requests / f"{label}.error.txt"
        if response_path.exists():
            try:
                output = output_type.model_validate_json(response_path.read_text(encoding="utf-8"))
            except ValidationError as exc:
                error_path.write_text(str(exc), encoding="utf-8")
                raise LLMPendingError(f"{response_path} no cumple el esquema {output_type.__name__} (detalle en {error_path})") from exc
            error_path.unlink(missing_ok=True)
            logger.info("Agente %s: respuesta leída de %s", label, response_path)
            return output

        request_path = self.requests / f"{label}.md"
        request_path.write_text(
            _REQUEST_TEMPLATE.format(
                label=label,
                response_path=response_path,
                instructions=instructions,
                prompt=prompt,
                model=output_type.__name__,
                schema=json.dumps(output_type.model_json_schema(), ensure_ascii=False, indent=1),
            ),
            encoding="utf-8",
        )
        raise LLMPendingError(f"Pendiente: {request_path} -> {response_path}")


RESULT_FILE = "result.json"


def _schema_for(label: str) -> type[BaseModel]:
    if label == "macro":
        return MacroAnalysis
    if label == "supervisor" or label.startswith("supervisor_"):
        return SupervisorOutput
    return AssetAnalysis


def handoff_status(directory: Path) -> dict:
    """Estado legible por máquina de una carpeta handoff (lo usa la skill /analisis de Claude Code).

    Por cada petición: `pending` (sin respuesta), `invalid` (no cumple el esquema) o `answered`.
    `waiting_for` son las etiquetas que necesitan respuesta; `complete` indica que el informe ya se generó.
    """
    requests, responses = directory / "requests", directory / "responses"
    agents: dict[str, dict] = {}
    for request in sorted(requests.glob("*.md")) if requests.exists() else []:
        label = request.stem
        response = responses / f"{label}.json"
        entry: dict = {"request": str(request), "response": str(response), "state": "pending"}
        if response.exists():
            try:
                parsed = _schema_for(label).model_validate_json(response.read_text(encoding="utf-8"))
            except ValidationError as exc:
                entry.update(state="invalid", error=str(exc))
            else:
                entry["state"] = "answered"
                if isinstance(parsed, AssetAnalysis) and parsed.asset_id != label:
                    entry.update(state="invalid", error=f"asset_id debe ser {label!r}, no {parsed.asset_id!r}")
        agents[label] = entry
    result = directory / RESULT_FILE
    return {
        "directory": str(directory),
        "started": (directory / "state.json").exists(),
        "complete": result.exists(),
        "result": json.loads(result.read_text(encoding="utf-8")) if result.exists() else None,
        "agents": agents,
        "waiting_for": [label for label, e in agents.items() if e["state"] != "answered"],
    }
