"""Tests de guardrails y asignación de importes."""

from datetime import date

from src.analysis.schemas import Claim
from src.config.models import RulesConfig
from src.models import AssetDecision, AssetSnapshot, DataQuality
from src.synthesis import allocation, guardrails
from tests.conftest import make_analysis


def _snap(make_prices, level="good", **price_overrides):
    return AssetSnapshot(asset_id="a", name="A", ticker="A", asset_class="equity", prices=make_prices(**price_overrides), quality=DataQuality(level=level))


def _decide(make_prices, macro, analysis=None, level="good", blocked=False, **price_overrides):
    return guardrails.apply_decision_rules(
        (analysis or make_analysis("a")).proposed_action, asset_id="a", is_equity=True, snapshot=_snap(make_prices, level, **price_overrides),
        analysis=analysis or make_analysis("a"), macro=macro, rules=RulesConfig(), blocked=blocked,
    )


def test_healthy_case_keeps_action(make_prices, macro):
    action, events = _decide(make_prices, macro)
    assert action == "normal_extra" and not events


def test_low_score_forces_no_extra(make_prices, macro):
    action, _ = _decide(make_prices, macro, make_analysis("a", opportunity_score=4))
    assert action == "no_extra"


def test_poor_data_and_blocked_force_no_extra(make_prices, macro):
    assert _decide(make_prices, macro, level="poor")[0] == "no_extra"
    assert _decide(make_prices, macro, blocked=True)[0] == "no_extra"


def test_momentum_chasing_capped(make_prices, macro):
    analysis = make_analysis("a", valuation="expensive", proposed_action="staged_extra")
    action, events = _decide(make_prices, macro, analysis, returns_report_ccy_pct={"1m": 5, "3m": 10, "6m": 20, "12m": 45})
    assert action == "small_extra"
    assert any(e.rule == "momentum_chasing" for e in events)


def test_cheap_only_capped(make_prices, macro):
    """Barato pero bajista, sentimiento desconocido y macro de riesgo alto: solo una señal."""
    risky = macro.model_copy(update={"equity_risk_level": "high"})
    analysis = make_analysis("a", valuation="cheap", sentiment="insufficient_data")
    action, events = _decide(make_prices, risky, analysis, quant_trend="down")
    assert action == "small_extra"
    assert any(e.rule == "single_signal" for e in events)


def test_no_macro_caps_small(make_prices):
    action, _ = _decide(make_prices, None)
    assert action == "small_extra"


def test_forbidden_language_detection_respects_negation():
    events, block = guardrails.scan_language(["Recomendamos usar apalancamiento x2"], "a", recommendation_context=True)
    assert block and events[0].severity == "blocking"
    events, block = guardrails.scan_language(["No se recomienda vender ni usar apalancamiento."], "a", recommendation_context=True)
    assert not block and not events


def test_fact_without_valid_source_is_downgraded():
    claims, events = guardrails.validate_claims([Claim(text="x", kind="fact", source_ids=["S99"])], {"S1"}, "a")
    assert claims[0].kind == "interpretation" and claims[0].source_ids == []
    assert {e.rule for e in events} == {"unknown_source", "fact_without_source"}


def _decision(asset_id, action, score):
    return AssetDecision(asset_id=asset_id, name=asset_id, opportunity_score=score, conviction="medium", analyst_action=action,
                         supervisor_action=action, proposed_action=action, final_action=action)


def test_allocation_respects_budget_and_max_assets():
    rules = RulesConfig(tactical_budget_eur=300, max_single_buy_eur=300, max_assets_with_extra=2)
    decisions = [_decision("a", "staged_extra", 8), _decision("b", "normal_extra", 9), _decision("c", "small_extra", 7)]
    allocation.allocate(decisions, rules, date(2026, 11, 16))
    assert sum(d.amount_eur for d in decisions) <= 300
    assert decisions[0].amount_eur == 300 and len(decisions[0].tranches) == 3
    assert sum(t.amount_eur for t in decisions[0].tranches) == 300
    assert decisions[1].final_action == "no_extra"  # presupuesto agotado
    assert decisions[2].final_action == "no_extra"  # límite de activos o presupuesto


def test_allocation_amounts_by_tier():
    rules = RulesConfig()
    decisions = [_decision("a", "small_extra", 7)]
    allocation.allocate(decisions, rules, date(2026, 11, 16))
    assert decisions[0].amount_eur == 90  # 300 * 0.33 = 99 -> redondeo a 10 hacia abajo
