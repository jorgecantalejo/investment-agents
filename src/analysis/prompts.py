"""Instrucciones de los agentes y construcción de los mensajes de entrada (en español)."""

from __future__ import annotations

import json
from typing import Any

from src.config.models import AppConfig, AssetConfig
from src.models import INSUFFICIENT, AssetSnapshot, MarketIndicator, Source

COMMON_RULES = """
Reglas obligatorias:
- Eres un asistente de investigación para inversores de largo plazo que operan en euros. Tu tarea es valorar si es
  buen momento para invertir en cada activo. No eres un trader y no conoces la situación personal del lector.
- NO calcules métricas financieras: usa exclusivamente los datos numéricos que te da Python. Si un dato vale
  "insufficient_data", trátalo como desconocido y dilo; nunca lo estimes ni lo inventes.
- Toda afirmación factual debe citar fuentes por su id (p. ej. "S4") de la lista FUENTES. Si no hay fuente,
  márcala como kind="interpretation" o no la incluyas. No inventes ids ni URLs.
- Distingue hechos (kind="fact") de interpretaciones (kind="interpretation").
- Prohibido: recomendar apalancamiento, recomendar vender posiciones, predecir precios a corto plazo,
  dar precios objetivo y usar lenguaje de certeza ("seguro", "garantizado", "sin riesgo").
- No recomiendes comprar solo porque un activo haya subido, ni solo porque esté barato: exige varias señales coherentes.
- Si los datos son incompletos, contradictorios o poco fiables, reduce la convicción y la acción propuesta.
- Escribe todos los textos en español, con frases concisas.
""".strip()

MACRO_INSTRUCTIONS = f"""
Eres un analista macroeconómico prudente.
Analiza crecimiento, inflación, tipos de interés, política monetaria, liquidez y riesgo global a partir de las
FUENTES proporcionadas y de los INDICADORES DE MERCADO calculados por Python (VIX, tipos, dólar, índices de miedo y
codicia, series oficiales de FRED: tipos Fed y BCE, curva, tipo real, inflación, paro, diferencial high yield, NFCI, y
el CAPE del S&P 500). Para las tendencias de inflación, tipos y liquidez da prioridad a las series de FRED frente a
las noticias. Clasifica el régimen económico y asigna el nivel de riesgo para la renta variable. Los indicadores no son
fuentes web: no los cites con ids, menciónalos como dato en afirmaciones kind="interpretation".
Extrae próximos eventos relevantes (bancos centrales, datos macro, resultados) solo si aparecen en las fuentes,
con su fecha; si la fecha no consta, usa "sin_fecha_confirmada".
En data_gaps enumera lo que no has podido verificar.

{COMMON_RULES}
""".strip()

ASSET_INSTRUCTIONS = f"""
Eres un analista multiactivo. Recibes DATOS OBJETIVOS calculados por Python, el contexto macro y FUENTES recientes.
Evalúa valuación, momentum y sentimiento; asigna una puntuación (0-10) de lo favorable que es el momento actual para
invertir en el activo con horizonte de largo plazo, y propone una acción acotada:
- no_extra: esperar; no es buen momento para entrar.
- small_extra: entrada pequeña.
- normal_extra: entrada normal.
- staged_extra: entrada escalonada en varios tramos.
Si la valuación incluye CAPE con percentiles históricos (percentiles.cape = toda la historia, cape_30y = 30 años),
úsalos como referencia principal: percentil >= 80 no es compatible con 'cheap' ni <= 20 con 'expensive'.
La etiqueta 'momentum' debe ser coherente con la tendencia cuantitativa (quant_trend) salvo que lo justifiques.
Indica condiciones de entrada, riesgos e invalidación de la tesis. En missing_data enumera los datos que faltan.
El campo asset_id debe ser exactamente el recibido.

{COMMON_RULES}
""".strip()

SUPERVISOR_INSTRUCTIONS = f"""
Eres el supervisor de un informe de inversión de largo plazo que valora si es buen momento para invertir en cada
activo. Recibes el análisis macro, los análisis por activo, los indicadores de mercado y los datos objetivos.
Tu tarea:
1. Resume la situación global en un resumen ejecutivo legible en 1 minuto.
2. Detecta contradicciones entre agentes, o entre análisis y datos objetivos, y explica cómo las resuelves.
3. Ordena los activos con mejor momento relativo para invertir (asset_ids) y señala dónde NO conviene entrar ahora y por qué.
4. Da una recomendación por activo (una acción por asset_id recibido) con condiciones e invalidación.
5. Redacta un plan de ejecución breve (sin importes: Python reparte el capital de referencia con reglas fijas).
No hagas referencia a la situación personal del lector ni a planes de aportación. Ante la duda, sé conservador.

{COMMON_RULES}
""".strip()


def _na(value: Any) -> Any:
    """Sustituye None por 'insufficient_data' de forma recursiva para el LLM."""
    if value is None:
        return INSUFFICIENT
    if isinstance(value, dict):
        return {k: _na(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_na(v) for v in value]
    return value


def format_sources(sources: list[Source]) -> str:
    if not sources:
        return "FUENTES: ninguna disponible (búsqueda web no realizada o sin resultados). No cites fuentes."
    lines = ["FUENTES:"]
    for s in sources:
        lines.append(f"[{s.id}] {s.title} | fecha: {s.published_date or 'desconocida'} | {s.url}\n    {s.snippet}")
    return "\n".join(lines)


def snapshot_payload(snapshot: AssetSnapshot) -> dict[str, Any]:
    data = snapshot.model_dump(exclude={"name", "ticker"})
    return _na(data)


def format_indicators(indicators: list[MarketIndicator]) -> str:
    if not indicators:
        return "INDICADORES DE MERCADO: no disponibles."
    lines = ["INDICADORES DE MERCADO (calculados por Python; no son fuentes web):"]
    for ind in indicators:
        value = INSUFFICIENT if ind.value is None else f"{ind.value}{ind.unit}"
        rating = f" ({ind.rating})" if ind.rating else ""
        previous = ", ".join(f"hace {k}: {INSUFFICIENT if v is None else v}" for k, v in ind.previous.items())
        lines.append(f"- {ind.name}: {value}{rating} a {ind.as_of or 'fecha desconocida'}" + (f" | {previous}" if previous else "") + f" | {ind.note}")
    return "\n".join(lines)


def macro_prompt(today: str, sources: list[Source], indicators: list[MarketIndicator]) -> str:
    return f"Fecha de hoy: {today}.\nAnaliza el entorno macro global actual.\n\n{format_indicators(indicators)}\n\n{format_sources(sources)}"


def asset_prompt(
    asset: AssetConfig, snapshot: AssetSnapshot, macro_brief: str, sources: list[Source], config: AppConfig, today: str,
    indicators: list[MarketIndicator],
) -> str:
    return f"""Fecha de hoy: {today}.
Activo: {asset.name} | asset_id: {asset.id} | ticker: {asset.ticker} | clase: {asset.asset_class} | categoría: {asset.category}
Notas de configuración: {asset.notes or 'ninguna'}
Horizonte de referencia: {config.rules.horizon_years} años. Moneda del informe: {config.report.currency}.

{format_indicators(indicators)}

CONTEXTO MACRO (resumen):
{macro_brief}

DATOS OBJETIVOS (calculados por Python; rentabilidades en %, 'pp' = puntos porcentuales vs benchmark):
{json.dumps(snapshot_payload(snapshot), ensure_ascii=False, indent=1)}

{format_sources(sources)}
"""


def supervisor_prompt(payload: dict[str, Any], sources: list[Source], today: str) -> str:
    return f"""Fecha de hoy: {today}.
ENTRADA (JSON):
{json.dumps(_na(payload), ensure_ascii=False, indent=1)}

{format_sources(sources)}
"""
