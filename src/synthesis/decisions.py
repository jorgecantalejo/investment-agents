"""Síntesis final: combina analista, supervisor y guardrails en decisiones con importe."""

from __future__ import annotations

from datetime import date

from src.analysis.schemas import AssetAnalysis, AvoidNote, MacroAnalysis, SupervisorOutput, SupervisorRecommendation
from src.config.models import AppConfig, AssetConfig
from src.models import AssetDecision, AssetSnapshot, GuardrailEvent
from src.synthesis import allocation, guardrails


def fallback_supervisor(analyses: dict[str, AssetAnalysis], macro: MacroAnalysis | None, assets: list[AssetConfig], min_score: int, reason: str) -> SupervisorOutput:
    """Síntesis determinista cuando el supervisor LLM no está disponible."""
    ranked = sorted(analyses.values(), key=lambda a: a.opportunity_score, reverse=True)
    best = [a.asset_id for a in ranked if a.opportunity_score >= min_score]
    avoid = [AvoidNote(asset_id=a.id, reason="Sin análisis disponible" if a.id not in analyses else f"Puntuación {analyses[a.id].opportunity_score}/10 por debajo del mínimo")
             for a in assets if a.id not in analyses or analyses[a.id].opportunity_score < min_score]
    return SupervisorOutput(
        executive_summary=f"Síntesis automática sin supervisor LLM ({reason}). Se ordenan los activos por la puntuación de sus analistas y se aplican las reglas de prudencia.",
        market_regime_summary=macro.summary if macro else "Sin análisis macro disponible.",
        overall_risk=macro.equity_risk_level if macro else "high",
        key_takeaways=["Síntesis automática: revisar manualmente antes de invertir."],
        best_opportunities=best,
        avoid_extra=avoid,
        contradictions=[],
        recommendations=[
            SupervisorRecommendation(asset_id=a.asset_id, action=a.proposed_action, rationale=a.summary, conditions=a.entry_conditions, invalidation=a.invalidation_conditions)
            for a in ranked
        ],
        execution_plan="Sin plan de supervisor: seguir las decisiones de la tabla final, que ya incorporan los límites de presupuesto.",
    )


def build_decisions(
    config: AppConfig,
    assets: list[AssetConfig],
    snapshots: dict[str, AssetSnapshot],
    analyses: dict[str, AssetAnalysis],
    supervisor: SupervisorOutput | None,
    macro: MacroAnalysis | None,
    blocked: set[str],
    today: date,
) -> tuple[list[AssetDecision], list[GuardrailEvent]]:
    events: list[GuardrailEvent] = []
    recs = {r.asset_id: r for r in supervisor.recommendations} if supervisor else {}
    avoid = {n.asset_id for n in supervisor.avoid_extra} if supervisor else set()
    decisions: list[AssetDecision] = []

    for asset in assets:
        snap = snapshots[asset.id]
        analysis = analyses.get(asset.id)
        rec = recs.get(asset.id)
        analyst_action = analysis.proposed_action if analysis else None
        sup_action = rec.action if rec else None
        if asset.id in avoid and sup_action and sup_action != "no_extra":
            events.append(GuardrailEvent(rule="supervisor_avoid", asset_id=asset.id, before=sup_action, after="no_extra", message="El supervisor indica que no conviene entrar ahora"))
            sup_action = "no_extra"

        proposed = analyst_action or "no_extra"
        if sup_action:
            proposed = guardrails.more_conservative(proposed, sup_action)
        final, rule_events = guardrails.apply_decision_rules(
            proposed, asset_id=asset.id, is_equity=asset.asset_class == "equity", snapshot=snap,
            analysis=analysis, macro=macro, rules=config.rules, blocked=asset.id in blocked,
        )
        events += rule_events
        decisions.append(
            AssetDecision(
                asset_id=asset.id,
                name=asset.name,
                opportunity_score=analysis.opportunity_score if analysis else None,
                conviction=analysis.conviction if analysis else None,
                analyst_action=analyst_action,
                supervisor_action=rec.action if rec else None,
                proposed_action=proposed,
                final_action=final,
                rationale=(rec.rationale if rec else (analysis.summary if analysis else "Sin análisis cualitativo.")),
                conditions=(rec.conditions if rec else (analysis.entry_conditions if analysis else [])),
                invalidation=(rec.invalidation if rec else (analysis.invalidation_conditions if analysis else [])),
            )
        )
    events += allocation.allocate(decisions, config.rules, today)
    return decisions, events
