"""Traduce acciones a importes respetando presupuesto, nº máximo de activos y tramos."""

from __future__ import annotations

from datetime import date, timedelta

from src.config.models import RulesConfig
from src.models import AssetDecision, GuardrailEvent, Tranche
from src.synthesis.guardrails import rank


def _round_down(value: float, step: float) -> float:
    return float(int(value // step) * step)


def allocate(decisions: list[AssetDecision], rules: RulesConfig, today: date) -> list[GuardrailEvent]:
    """Asigna importes in-place. Prioriza por acción y puntuación; nunca supera el presupuesto."""
    events: list[GuardrailEvent] = []
    candidates = sorted(
        (d for d in decisions if d.final_action != "no_extra"),
        key=lambda d: (rank(d.final_action), d.opportunity_score or 0),
        reverse=True,
    )
    remaining = rules.tactical_budget_eur
    funded = 0
    for decision in candidates:
        if funded >= rules.max_assets_with_extra:
            events.append(GuardrailEvent(rule="max_assets", asset_id=decision.asset_id, before=decision.final_action, after="no_extra",
                                         message=f"Límite de {rules.max_assets_with_extra} activos con extra alcanzado"))
            decision.final_action = "no_extra"
            continue
        fraction = getattr(rules.tier_fractions, decision.final_action)
        wanted = _round_down(rules.max_single_buy_eur * fraction, rules.rounding_eur)
        amount = min(wanted, _round_down(remaining, rules.rounding_eur))
        if amount < rules.min_ticket_eur:
            events.append(GuardrailEvent(rule="budget_exhausted", asset_id=decision.asset_id, before=decision.final_action, after="no_extra",
                                         message=f"Presupuesto restante ({remaining:.0f} EUR) por debajo del ticket mínimo"))
            decision.final_action = "no_extra"
            continue
        if amount < wanted:
            events.append(GuardrailEvent(rule="budget_cap", asset_id=decision.asset_id, severity="info",
                                         message=f"Importe reducido de {wanted:.0f} a {amount:.0f} EUR por presupuesto total"))
        decision.amount_eur = amount
        remaining -= amount
        funded += 1
        if decision.final_action == "staged_extra":
            n = rules.staged_tranches
            base = _round_down(amount / n, rules.rounding_eur)
            amounts = [base] * (n - 1) + [round(amount - base * (n - 1), 2)]
            decision.tranches = [
                Tranche(amount_eur=a, planned_date=(today + timedelta(days=i * rules.staged_interval_days)).isoformat())
                for i, a in enumerate(amounts)
            ]
        else:
            decision.tranches = [Tranche(amount_eur=amount, planned_date=today.isoformat())]
    return events
