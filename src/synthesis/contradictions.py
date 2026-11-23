"""Detección determinista de señales contradictorias entre agentes y datos objetivos."""

from __future__ import annotations

from src.analysis.schemas import AssetAnalysis, MacroAnalysis, SupervisorOutput
from src.models import AssetDecision, AssetSnapshot, Contradiction
from src.synthesis.guardrails import rank


def detect(
    snapshots: dict[str, AssetSnapshot],
    analyses: dict[str, AssetAnalysis],
    decisions: list[AssetDecision],
    macro: MacroAnalysis | None,
    supervisor: SupervisorOutput | None,
) -> list[Contradiction]:
    found: list[Contradiction] = []
    for asset_id, analysis in analyses.items():
        snap = snapshots.get(asset_id)
        trend = snap.prices.quant_trend if snap and snap.prices else "insufficient_data"
        if trend != "insufficient_data" and analysis.momentum not in (trend, "insufficient_data"):
            found.append(Contradiction(asset_ids=[asset_id], description=f"El analista califica el momentum como '{analysis.momentum}' pero la tendencia calculada es '{trend}'.",
                                       resolution="Prevalece el dato cuantitativo; revisar la justificación del analista."))
        cape_pct = snap.valuation.percentiles.get("cape") if snap and snap.valuation else None
        if cape_pct is not None and ((cape_pct >= 80 and analysis.valuation == "cheap") or (cape_pct <= 20 and analysis.valuation == "expensive")):
            found.append(Contradiction(asset_ids=[asset_id], description=f"El analista califica la valuación como '{analysis.valuation}' pero el CAPE está en el percentil {cape_pct:.0f} de su historia.",
                                       resolution="Prevalece el dato histórico; revisar la justificación del analista."))
        if analysis.valuation == "expensive" and analysis.opportunity_score >= 7:
            found.append(Contradiction(asset_ids=[asset_id], description=f"Valuación 'cara' con puntuación alta ({analysis.opportunity_score}/10).",
                                       resolution="Los guardrails limitan la acción si no hay más señales de apoyo."))
        if macro and macro.equity_risk_level == "high" and snap and snap.asset_class == "equity" and rank(analysis.proposed_action) >= rank("normal_extra"):
            found.append(Contradiction(asset_ids=[asset_id], description="El agente macro ve riesgo alto para renta variable, pero el analista propone una entrada normal o mayor.",
                                       resolution="Se aplica la acción más conservadora entre analista y supervisor."))

    for d in decisions:
        if d.analyst_action and d.supervisor_action and abs(rank(d.analyst_action) - rank(d.supervisor_action)) >= 2:
            found.append(Contradiction(asset_ids=[d.asset_id], origin="python",
                                       description=f"Analista ('{d.analyst_action}') y supervisor ('{d.supervisor_action}') difieren en dos o más niveles.",
                                       resolution=f"Se toma la más conservadora: '{d.proposed_action}'."))

    if supervisor:
        both = set(supervisor.best_opportunities) & {n.asset_id for n in supervisor.avoid_extra}
        for asset_id in sorted(both):
            found.append(Contradiction(asset_ids=[asset_id], description="El supervisor lo lista a la vez como oportunidad y como activo a evitar.",
                                       resolution="Se trata como «esperar» por prudencia."))
        for c in supervisor.contradictions:
            found.append(Contradiction(description=c.description, asset_ids=c.asset_ids, origin="supervisor", resolution=c.resolution))
    return found
