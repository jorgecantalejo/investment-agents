"""Prepara el contexto de la plantilla a partir de un `RunRecord` (sin lógica de negocio)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from src.models import RunRecord, Source

ACTION_LABELS = {
    "no_extra": "Esperar",
    "small_extra": "Entrada pequeña",
    "normal_extra": "Entrada normal",
    "staged_extra": "Entrada escalonada",
}
TREND_STATE_LABELS = {
    "sin_posicion": "Sin posición",
    "entrada_pendiente": "Entrada pendiente",
    "mantener": "Mantener",
    "salida_pendiente": "Salida pendiente",
    "insufficient_data": "insufficient_data",
}
LEVEL_LABELS = {"low": "Bajo", "medium": "Medio", "high": "Alto"}
LEVEL_LABELS_F = {"low": "Baja", "medium": "Media", "high": "Alta"}  # para "convicción", "confianza"
TERM_LABELS = {
    "expansion": "Expansión", "late_cycle": "Ciclo tardío", "slowdown": "Desaceleración", "recession": "Recesión",
    "recovery": "Recuperación", "unclear": "Incierto", "accelerating": "Acelerando", "stable": "Estable",
    "decelerating": "Desacelerando", "falling": "A la baja", "rising": "Al alza", "loose": "Holgada",
    "neutral": "Neutral", "tight": "Restrictiva", "cheap": "Barato", "fair": "Razonable", "expensive": "Caro",
    "up": "Alcista", "sideways": "Lateral", "down": "Bajista", "fearful": "Miedo", "euphoric": "Euforia",
    "insufficient_data": "insufficient_data", "good": "Buena", "partial": "Parcial", "poor": "Pobre",
    "unavailable": "No disponible",
}


def build_context(record: RunRecord, title: str, rules: dict[str, Any]) -> dict[str, Any]:
    source_map: dict[str, Source] = {s.id: s for s in record.sources}
    decisions = {d.asset_id: d for d in record.decisions}
    names = {s.asset_id: s.name for s in record.snapshots}

    cards = []
    for snap in record.snapshots:
        analysis = record.asset_analyses.get(snap.asset_id)
        cited = {sid for c in (analysis.claims if analysis else []) for sid in c.source_ids}
        sources = [s for s in record.sources if s.scope == snap.asset_id or s.id in cited]
        cards.append({"snapshot": snap, "analysis": analysis, "decision": decisions.get(snap.asset_id), "sources": sources})

    missing_by_scope: dict[str, list] = defaultdict(list)
    for item in record.missing_data:
        missing_by_scope[names.get(item.scope, item.scope)].append(item)

    sup = record.supervisor
    indicators = {i.id: i for i in record.indicators}
    budget = rules.get("tactical_budget_eur") or 0
    return {
        "indicators": indicators,
        "budget": budget,
        "allocated_pct": round(record.total_extra_eur / budget * 100) if budget else 0,
        "title": title,
        "run": record,
        "date": record.created_at[:10],
        "currency": record.report_currency,
        "rules": rules,
        "macro": record.macro,
        "supervisor": sup,
        "cards": cards,
        "decisions": record.decisions,
        "funded": [d for d in record.decisions if d.amount_eur > 0],
        "total_extra": record.total_extra_eur,
        "best": [names.get(a, a) for a in (sup.best_opportunities if sup else [])],
        "avoid": [(names.get(n.asset_id, n.asset_id), n.reason) for n in (sup.avoid_extra if sup else [])],
        "contradictions": record.contradictions,
        "missing_by_scope": dict(missing_by_scope),
        "guardrails": record.guardrail_events,
        "sources": record.sources,
        "source_map": source_map,
        "macro_sources": [s for s in record.sources if s.scope in ("macro", "events")],
        "names": names,
        "strategies": record.strategies,
    }
