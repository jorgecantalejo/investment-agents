"""Salidas estructuradas (Pydantic) que devuelven los agentes LLM.

Los modelos no tienen valores por defecto para ser compatibles con el modo
estricto de structured outputs: el LLM debe rellenar todos los campos.
Las fuentes se citan por id (S1, S2...) de la lista que Python le entrega.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ActionLiteral = Literal["no_extra", "small_extra", "normal_extra", "staged_extra"]
Level = Literal["low", "medium", "high"]


class Claim(BaseModel):
    """Afirmación etiquetada como hecho (requiere fuente) o interpretación."""

    text: str
    kind: Literal["fact", "interpretation"]
    source_ids: list[str] = Field(description="Ids de fuente (p. ej. 'S3'). Obligatorio si kind='fact'.")


class UpcomingEvent(BaseModel):
    date: str = Field(description="Fecha AAAA-MM-DD o 'sin_fecha_confirmada'")
    event: str
    relevance: str = Field(description="Por qué importa y a qué activos afecta")
    source_ids: list[str]


class MacroAnalysis(BaseModel):
    regime: Literal["expansion", "late_cycle", "slowdown", "recession", "recovery", "unclear"]
    growth_trend: Literal["accelerating", "stable", "decelerating", "unclear"]
    inflation_trend: Literal["falling", "stable", "rising", "unclear"]
    rates_trend: Literal["falling", "stable", "rising", "unclear"]
    liquidity: Literal["loose", "neutral", "tight", "unclear"]
    equity_risk_level: Level
    equity_risk_score: int = Field(ge=0, le=10, description="0 = riesgo mínimo, 10 = riesgo máximo para renta variable")
    confidence: Level
    summary: str
    claims: list[Claim]
    key_risks: list[str]
    upcoming_events: list[UpcomingEvent]
    data_gaps: list[str]


class AssetAnalysis(BaseModel):
    asset_id: str
    valuation: Literal["cheap", "fair", "expensive", "insufficient_data"]
    valuation_rationale: str
    momentum: Literal["up", "sideways", "down", "insufficient_data"]
    momentum_rationale: str
    sentiment: Literal["fearful", "neutral", "euphoric", "insufficient_data"]
    sentiment_rationale: str
    opportunity_score: int = Field(ge=0, le=10)
    conviction: Level
    proposed_action: ActionLiteral
    summary: str
    claims: list[Claim]
    key_risks: list[str]
    invalidation_conditions: list[str]
    entry_conditions: list[str]
    missing_data: list[str]


class AvoidNote(BaseModel):
    asset_id: str
    reason: str


class SupervisorContradiction(BaseModel):
    description: str
    asset_ids: list[str]
    resolution: str


class SupervisorRecommendation(BaseModel):
    asset_id: str
    action: ActionLiteral
    rationale: str
    conditions: list[str]
    invalidation: list[str]


class SupervisorOutput(BaseModel):
    executive_summary: str
    market_regime_summary: str
    overall_risk: Level
    key_takeaways: list[str]
    best_opportunities: list[str] = Field(description="asset_ids ordenados de mejor a peor")
    avoid_extra: list[AvoidNote]
    contradictions: list[SupervisorContradiction]
    recommendations: list[SupervisorRecommendation]
    execution_plan: str
