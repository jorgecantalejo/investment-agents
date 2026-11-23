"""Definición de los agentes (OpenAI Agents SDK). No tienen herramientas: reciben todo el contexto
ya preparado por Python, lo que los hace baratos, reproducibles y auditables."""

from __future__ import annotations

from agents import Agent, AgentOutputSchema

from src.analysis import prompts
from src.analysis.schemas import AssetAnalysis, MacroAnalysis, SupervisorOutput


def build_agents(model: str | None) -> dict[str, Agent]:
    kwargs = {"model": model} if model else {}
    return {
        "macro": Agent(
            name="Analista macro",
            instructions=prompts.MACRO_INSTRUCTIONS,
            output_type=AgentOutputSchema(MacroAnalysis, strict_json_schema=True),
            **kwargs,
        ),
        "asset": Agent(
            name="Analista de activo",
            instructions=prompts.ASSET_INSTRUCTIONS,
            output_type=AgentOutputSchema(AssetAnalysis, strict_json_schema=True),
            **kwargs,
        ),
        "supervisor": Agent(
            name="Supervisor de cartera",
            instructions=prompts.SUPERVISOR_INSTRUCTIONS,
            output_type=AgentOutputSchema(SupervisorOutput, strict_json_schema=True),
            **kwargs,
        ),
    }
