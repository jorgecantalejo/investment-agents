"""Guardrails deterministas sobre las salidas del LLM.

El LLM propone; Python decide los límites. Cada ajuste queda registrado como
`GuardrailEvent` y se muestra en el informe para que sea auditable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from src.analysis.schemas import AssetAnalysis, Claim, MacroAnalysis, SupervisorOutput
from src.config.models import Action, RulesConfig
from src.models import AssetSnapshot, GuardrailEvent

ACTION_ORDER: list[Action] = ["no_extra", "small_extra", "normal_extra", "staged_extra"]

# (regex, descripción, bloqueante si aparece sin negación en una recomendación)
FORBIDDEN_PATTERNS: list[tuple[str, str, bool]] = [
    (r"apalanc|leverag|margin trading|\bcfds?\b|x[23] ", "apalancamiento", True),
    (r"\bvend(e|er|a|an|emos)\b|\bventa(s)?\b|liquidar posici", "venta de posiciones", True),
    (r"\bposiciones? cortas?\b|\bshort\b|ponerse corto", "posiciones cortas", True),
    (r"garantiz|sin riesgo|seguro que|\bsin duda\b|con certeza", "lenguaje de certeza", False),
    (r"precio objetivo|target price|llegar[áa] a \d|subir[áa] (hasta|a) \d", "predicción de precio", False),
]
_NEGATIONS = re.compile(r"\b(no|nunca|sin|evitar|evite|prohibido|ni)\b[^.;]{0,40}$", re.IGNORECASE)


def rank(action: Action) -> int:
    return ACTION_ORDER.index(action)


def cap(action: Action, maximum: Action) -> Action:
    return action if rank(action) <= rank(maximum) else maximum


def more_conservative(a: Action, b: Action) -> Action:
    return a if rank(a) <= rank(b) else b


def scan_language(texts: Iterable[str], asset_id: str | None, recommendation_context: bool) -> tuple[list[GuardrailEvent], bool]:
    """Busca lenguaje prohibido. Devuelve (eventos, ¿hay que bloquear la acción?)."""
    events: list[GuardrailEvent] = []
    block = False
    for text in texts:
        for pattern, label, blocking in FORBIDDEN_PATTERNS:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                negated = bool(_NEGATIONS.search(text[max(0, match.start() - 45): match.start()]))
                if negated:
                    continue
                is_block = blocking and recommendation_context
                block = block or is_block
                events.append(
                    GuardrailEvent(
                        rule="forbidden_language",
                        asset_id=asset_id,
                        severity="blocking" if is_block else "warning",
                        message=f"Posible {label}: «…{text[max(0, match.start() - 30): match.end() + 30].strip()}…»",
                    )
                )
                break
    return events, block


def validate_claims(claims: list[Claim], known_ids: set[str], scope: str) -> tuple[list[Claim], list[GuardrailEvent]]:
    """Elimina ids de fuente inexistentes y degrada a interpretación los 'hechos' sin fuente válida."""
    events: list[GuardrailEvent] = []
    cleaned: list[Claim] = []
    for claim in claims:
        valid = [s for s in claim.source_ids if s in known_ids]
        invalid = [s for s in claim.source_ids if s not in known_ids]
        if invalid:
            events.append(GuardrailEvent(rule="unknown_source", asset_id=scope, message=f"Ids de fuente inexistentes eliminados: {invalid}"))
        kind = claim.kind
        if kind == "fact" and not valid:
            kind = "interpretation"
            events.append(
                GuardrailEvent(rule="fact_without_source", asset_id=scope, message=f"Hecho sin fuente reclasificado como interpretación: «{claim.text[:90]}»")
            )
        cleaned.append(Claim(text=claim.text, kind=kind, source_ids=valid))
    return cleaned, events


def sanitize_macro(macro: MacroAnalysis, known_ids: set[str]) -> tuple[MacroAnalysis, list[GuardrailEvent]]:
    claims, events = validate_claims(macro.claims, known_ids, "macro")
    events_list = []
    for ev in macro.upcoming_events:
        valid = [s for s in ev.source_ids if s in known_ids]
        if not valid:
            events.append(GuardrailEvent(rule="event_without_source", asset_id="macro", severity="info", message=f"Evento sin fuente verificable descartado: {ev.event}"))
            continue
        events_list.append(ev.model_copy(update={"source_ids": valid}))
    lang, _ = scan_language([macro.summary, *[c.text for c in claims]], "macro", recommendation_context=False)
    return macro.model_copy(update={"claims": claims, "upcoming_events": events_list}), events + lang


def sanitize_asset(analysis: AssetAnalysis, expected_id: str, known_ids: set[str]) -> tuple[AssetAnalysis, list[GuardrailEvent], bool]:
    """Valida un análisis de activo. Devuelve (análisis limpio, eventos, ¿bloquear extra?)."""
    events: list[GuardrailEvent] = []
    if analysis.asset_id != expected_id:
        events.append(GuardrailEvent(rule="asset_id_mismatch", asset_id=expected_id, message=f"El LLM devolvió asset_id '{analysis.asset_id}'; corregido"))
    claims, claim_events = validate_claims(analysis.claims, known_ids, expected_id)
    lang, block = scan_language(
        [analysis.summary, *analysis.entry_conditions, analysis.valuation_rationale, analysis.sentiment_rationale],
        expected_id,
        recommendation_context=True,
    )
    cleaned = analysis.model_copy(update={"asset_id": expected_id, "claims": claims, "opportunity_score": max(0, min(10, analysis.opportunity_score))})
    return cleaned, events + claim_events + lang, block


def sanitize_supervisor(sup: SupervisorOutput, valid_assets: set[str]) -> tuple[SupervisorOutput, list[GuardrailEvent], set[str]]:
    """Elimina referencias a activos desconocidos y detecta lenguaje prohibido. Devuelve activos bloqueados."""
    events: list[GuardrailEvent] = []
    blocked: set[str] = set()

    def keep(asset_id: str, where: str) -> bool:
        if asset_id in valid_assets:
            return True
        events.append(GuardrailEvent(rule="unknown_asset", message=f"El supervisor citó un activo desconocido en {where}: '{asset_id}'"))
        return False

    recs = []
    for rec in sup.recommendations:
        if not keep(rec.asset_id, "recommendations"):
            continue
        lang, block = scan_language([rec.rationale, *rec.conditions], rec.asset_id, recommendation_context=True)
        events += lang
        if block:
            blocked.add(rec.asset_id)
        recs.append(rec)
    lang, _ = scan_language([sup.executive_summary, sup.execution_plan, *sup.key_takeaways], None, recommendation_context=False)
    events += lang
    cleaned = sup.model_copy(
        update={
            "recommendations": recs,
            "best_opportunities": [a for a in sup.best_opportunities if keep(a, "best_opportunities")],
            "avoid_extra": [n for n in sup.avoid_extra if keep(n.asset_id, "avoid_extra")],
        }
    )
    return cleaned, events, blocked


def apply_decision_rules(
    proposed: Action,
    *,
    asset_id: str,
    is_equity: bool,
    snapshot: AssetSnapshot,
    analysis: AssetAnalysis | None,
    macro: MacroAnalysis | None,
    rules: RulesConfig,
    blocked: bool,
) -> tuple[Action, list[GuardrailEvent]]:
    """Reglas personales y de prudencia aplicadas sobre la acción propuesta."""
    events: list[GuardrailEvent] = []
    action = proposed

    def limit(maximum: Action, rule: str, message: str, severity: str = "warning") -> None:
        nonlocal action
        new = cap(action, maximum)
        if new != action:
            events.append(GuardrailEvent(rule=rule, asset_id=asset_id, message=message, severity=severity, before=action, after=new))  # type: ignore[arg-type]
            action = new

    if blocked:
        limit("no_extra", "forbidden_recommendation", "La recomendación contenía apalancamiento/venta/cortos: anulada", "blocking")
    if snapshot.quality.level in ("unavailable", "poor"):
        limit("no_extra", "data_quality", f"Calidad de datos '{snapshot.quality.level}': no se recomienda entrar", "blocking")
    if analysis is None:
        limit("no_extra", "no_analysis", "Sin análisis cualitativo disponible", "blocking")
        return action, events

    if analysis.opportunity_score < rules.min_score_for_extra:
        limit("no_extra", "min_score", f"Puntuación {analysis.opportunity_score} < mínimo {rules.min_score_for_extra}")
    if analysis.conviction == "low":
        limit("small_extra", "low_conviction", "Convicción baja: máximo «entrada pequeña»")
    if snapshot.quality.level == "partial":
        limit("normal_extra", "partial_data", "Datos parciales: máximo «entrada normal»")
    if macro is None:
        limit("small_extra", "no_macro", "Sin análisis macro: máximo «entrada pequeña»")
    elif macro.confidence == "low":
        limit("normal_extra", "low_macro_confidence", "Confianza macro baja: máximo «entrada normal»")

    prices = snapshot.prices
    trend = prices.quant_trend if prices else "insufficient_data"
    ret12 = prices.returns_report_ccy_pct.get("12m") if prices else None
    if ret12 is None and prices:
        ret12 = prices.returns_pct.get("12m")

    # No comprar solo porque haya subido.
    if trend == "up" and ret12 is not None and ret12 >= rules.momentum_chase_return_12m_pct and analysis.valuation in ("expensive", "insufficient_data"):
        limit("small_extra", "momentum_chasing", f"Subida de {ret12:.1f}% en 12m sin valuación favorable verificada: máximo «entrada pequeña»")
    if analysis.sentiment == "euphoric":
        limit("small_extra", "euphoric_sentiment", "Sentimiento eufórico: máximo «entrada pequeña»")

    # Exigir varias señales coherentes (no comprar solo porque esté barato o solo porque suba).
    supportive = {
        "valuación razonable": analysis.valuation in ("cheap", "fair"),
        "sentimiento no eufórico verificado": analysis.sentiment in ("fearful", "neutral"),
        "tendencia no bajista": trend in ("up", "sideways"),
        "macro no adverso": macro is not None and (not is_equity or macro.equity_risk_level != "high"),
    }
    count = sum(supportive.values())
    if count < 2:
        limit("small_extra", "single_signal", f"Solo {count} señal(es) favorable(s) ({', '.join(k for k, v in supportive.items() if v) or 'ninguna'}): máximo «entrada pequeña»")
    if analysis.valuation == "cheap" and trend == "down" and count < 3:
        limit("small_extra", "value_trap_risk", "Barato pero en tendencia bajista y sin más apoyos: máximo «entrada pequeña»")
    return action, events
