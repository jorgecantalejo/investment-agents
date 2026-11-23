"""Fixtures compartidas: series sintéticas y un RunRecord completo sin red."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.analysis.schemas import AssetAnalysis, Claim, MacroAnalysis, SupervisorOutput, SupervisorRecommendation, UpcomingEvent
from src.config import parse_config
from src.models import AssetDecision, AssetSnapshot, Contradiction, DataQuality, MissingDataItem, PriceMetrics, RunRecord, Source, ValuationMetrics


@pytest.fixture(autouse=True)
def no_sentiment_network(monkeypatch):
    """Los tests nunca llaman a fuentes externas sin clave (miedo y codicia, FRED, CAPE): simulan que no responden."""
    from src.core.errors import NetworkError
    from src.market.providers import cape, fred, sentiment

    def offline(*args, **kwargs):
        raise NetworkError("sin red en tests")

    monkeypatch.setattr(sentiment, "fetch_stock_fear_greed", offline)
    monkeypatch.setattr(sentiment, "fetch_crypto_fear_greed", offline)
    monkeypatch.setattr(fred, "fetch_series", offline)
    monkeypatch.setattr(cape, "fetch_shiller_cape", offline)


@pytest.fixture
def linear_series() -> pd.Series:
    """Serie diaria (días hábiles) que crece 1 unidad por observación, de 100 a 399."""
    idx = pd.bdate_range("2024-01-01", periods=300)
    return pd.Series(np.arange(100, 400, dtype=float), index=idx)


@pytest.fixture
def minimal_config_dict() -> dict:
    return {
        "assets": [
            {"id": "msci_world", "name": "MSCI World", "ticker": "IWDA.AS", "asset_class": "equity", "currency": "EUR"},
            {"id": "gold", "name": "Oro", "ticker": "GC=F", "asset_class": "commodity"},
        ]
    }


@pytest.fixture
def config(minimal_config_dict):
    return parse_config(minimal_config_dict)


def _prices(**overrides) -> PriceMetrics:
    base = dict(
        currency="EUR", last_price=100.0, last_date="2026-11-13", observations=1200,
        returns_pct={"1m": 1.0, "3m": 2.0, "6m": 3.0, "12m": 10.0},
        returns_report_ccy_pct={"1m": 1.0, "3m": 2.0, "6m": 3.0, "12m": 10.0}, report_currency="EUR",
        sma50=98.0, sma200=95.0, above_sma50=True, above_sma200=True, recent_high=104.0, recent_high_date="2026-10-01",
        drawdown_from_recent_high_pct=-3.85, max_drawdown_window_pct=-8.0, volatility_annualized_pct=12.0,
        benchmark_ticker="^GSPC", benchmark_returns_report_ccy_pct={"1m": 0.5, "3m": 1.0, "6m": 2.0, "12m": 8.0},
        relative_vs_benchmark_pp={"1m": 0.5, "3m": 1.0, "6m": 1.0, "12m": 2.0}, quant_trend="up",
    )
    base.update(overrides)
    return PriceMetrics(**base)


@pytest.fixture
def make_prices():
    return _prices


def make_analysis(asset_id: str, **overrides) -> AssetAnalysis:
    base = dict(
        asset_id=asset_id, valuation="fair", valuation_rationale="PER en línea con su media.", momentum="up",
        momentum_rationale="Precio sobre SMA200.", sentiment="neutral", sentiment_rationale="Sin euforia.",
        opportunity_score=7, conviction="medium", proposed_action="normal_extra", summary="Resumen de prueba.",
        claims=[Claim(text="El BCE mantuvo tipos.", kind="fact", source_ids=["S1"])],
        key_risks=["Recesión global"], invalidation_conditions=["Cierre semanal bajo SMA200"],
        entry_conditions=["Mantener tendencia"], missing_data=[],
    )
    base.update(overrides)
    return AssetAnalysis(**base)


@pytest.fixture
def macro() -> MacroAnalysis:
    return MacroAnalysis(
        regime="late_cycle", growth_trend="decelerating", inflation_trend="stable", rates_trend="falling", liquidity="neutral",
        equity_risk_level="medium", equity_risk_score=5, confidence="medium", summary="Crecimiento moderado.",
        claims=[Claim(text="La Fed recortó tipos.", kind="fact", source_ids=["S1"])], key_risks=["Inflación persistente"],
        upcoming_events=[UpcomingEvent(date="2026-12-10", event="Reunión de la Fed", relevance="Tipos USD", source_ids=["S1"])],
        data_gaps=["PMI de servicios no verificado"],
    )


@pytest.fixture
def run_record(macro) -> RunRecord:
    snaps = [
        AssetSnapshot(asset_id="msci_world", name="MSCI World", ticker="IWDA.AS", asset_class="equity", prices=_prices(),
                      valuation=ValuationMetrics(provider="yfinance", source_ticker="URTH", trailing_pe=21.4, dividend_yield_pct=1.4),
                      quality=DataQuality(level="good")),
        AssetSnapshot(asset_id="bad", name="Ticker roto", ticker="NOPE", asset_class="other",
                      quality=DataQuality(level="unavailable", issues=["Sin datos"]), error="Sin datos para 'NOPE'"),
    ]
    analysis = make_analysis("msci_world")
    supervisor = SupervisorOutput(
        executive_summary="Entorno mixto.", market_regime_summary="Ciclo tardío.", overall_risk="medium",
        key_takeaways=["Mantener plan mensual"], best_opportunities=["msci_world"], avoid_extra=[], contradictions=[],
        recommendations=[SupervisorRecommendation(asset_id="msci_world", action="normal_extra", rationale="Tendencia sana.", conditions=["c"], invalidation=["i"])],
        execution_plan="Una compra esta semana.",
    )
    return RunRecord(
        run_id="test-run", created_at="2026-11-16T10:00:00", mode="full", report_currency="EUR",
        assets_requested=["msci_world", "bad"], config_snapshot={},
        snapshots=snaps, macro=macro, asset_analyses={"msci_world": analysis}, supervisor=supervisor,
        sources=[Source(id="S1", title="Fed <decision>", url="https://example.com/fed", published_date="2026-11-10",
                        retrieved_at="2026-11-16T09:00:00+00:00", query="fed", scope="macro")],
        decisions=[AssetDecision(asset_id="msci_world", name="MSCI World", opportunity_score=7, conviction="medium",
                                 analyst_action="normal_extra", supervisor_action="normal_extra", proposed_action="normal_extra",
                                 final_action="normal_extra", amount_eur=190, rationale="Tendencia sana.")],
        contradictions=[Contradiction(description="Momentum discrepante", asset_ids=["msci_world"], resolution="Prevalece el dato")],
        missing_data=[MissingDataItem(scope="msci_world", item="CAPE: insufficient_data")],
    )
