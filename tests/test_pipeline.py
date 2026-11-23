"""Test de integración del pipeline completo con proveedores y agentes simulados (sin red)."""

import asyncio
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src import pipeline as pipeline_mod
from src.analysis.schemas import AssetAnalysis, MacroAnalysis, SupervisorOutput, SupervisorRecommendation
from src.core.errors import InvalidTickerError
from src.models import ValuationMetrics
from src.pipeline import Pipeline, RunOptions
from src.reporting.html import render_report
from src.settings import Settings
from tests.conftest import make_analysis

ROOT = Path(__file__).resolve().parent.parent


class FakePrices:
    name = "fake"

    def get_close_history(self, ticker, period):
        if ticker == "NOPE":
            raise InvalidTickerError("Sin datos para 'NOPE'")
        idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=800)
        return pd.Series(100 * 1.0004 ** np.arange(800), index=idx, name=ticker)

    def get_valuation(self, ticker):
        return ValuationMetrics(provider="fake", source_ticker=ticker, trailing_pe=20.0)


class FakeSearch:
    name = "fake"

    def __init__(self, api_key):
        self.calls = 0

    def search(self, query, *, max_results, time_range, depth, include_domains=None):
        self.calls += 1
        return [{"title": f"Noticia {query[:20]}", "url": f"https://example.com/{abs(hash(query))}", "content": "texto", "published_date": "2026-11-10"}]


def test_full_pipeline_with_fakes(monkeypatch, tmp_path, config, macro):
    config.assets.append(config.assets[1].model_copy(update={"id": "broken", "ticker": "NOPE"}))
    monkeypatch.setattr(pipeline_mod, "YFinanceProvider", FakePrices)
    monkeypatch.setattr(pipeline_mod, "TavilyProvider", FakeSearch)

    async def fake_run(self, agent, prompt, output_type, label):
        if output_type is MacroAnalysis:
            return macro
        if output_type is AssetAnalysis:
            # El LLM "alucina" una fuente inexistente y propone mucho en el oro
            action = "staged_extra" if label == "gold" else "normal_extra"
            return make_analysis(label, proposed_action=action, opportunity_score=8)
        return SupervisorOutput(
            executive_summary="ok", market_regime_summary="ok", overall_risk="medium", key_takeaways=[],
            best_opportunities=["gold", "inventado"], avoid_extra=[], contradictions=[],
            recommendations=[SupervisorRecommendation(asset_id="gold", action="small_extra", rationale="r", conditions=[], invalidation=[])],
            execution_plan="plan",
        )

    monkeypatch.setattr(pipeline_mod.AgentRunner, "run", fake_run)
    monkeypatch.setattr(pipeline_mod.AnthropicRunner, "run", fake_run)
    settings = Settings(openai_api_key="x", tavily_api_key="y", openai_model=None, project_root=tmp_path)
    record = asyncio.run(Pipeline(config, settings, RunOptions(), "t1").run())

    decisions = {d.asset_id: d for d in record.decisions}
    assert decisions["broken"].final_action == "no_extra"
    assert decisions["gold"].final_action == "small_extra"          # se toma el más conservador (supervisor)
    assert record.total_extra_eur <= config.rules.tactical_budget_eur
    assert record.search_usage.performed <= config.research.max_searches_per_run
    assert any(e.rule == "unknown_asset" for e in record.guardrail_events)
    assert any("broken" in m.scope for m in record.missing_data)
    assert any(c.origin == "python" for c in record.contradictions)  # analista staged vs supervisor small
    html = render_report(record, ROOT / "templates", "t", config.rules.model_dump())
    assert "Tabla final de recomendaciones" in html
    ind = {i.id: i for i in record.indicators}
    assert ind["fear_greed_stocks"].value is None and ind["vix"].value is not None  # fallo aislado por indicador
    assert all(s.prices.sparkline for s in record.snapshots if s.prices)

    # Segunda ejecución: las búsquedas salen de caché
    record2 = asyncio.run(Pipeline(config, settings, RunOptions(), "t2").run())
    assert record2.search_usage.performed == 0 and record2.search_usage.cache_hits > 0


def test_quant_mode_never_recommends_extra(monkeypatch, tmp_path, config):
    monkeypatch.setattr(pipeline_mod, "YFinanceProvider", FakePrices)
    settings = Settings(openai_api_key=None, tavily_api_key=None, openai_model=None, project_root=tmp_path)
    record = asyncio.run(Pipeline(config, settings, RunOptions(mode="quant"), "t3").run())
    assert record.total_extra_eur == 0
    assert all(s.prices for s in record.snapshots)


def test_missing_anthropic_credentials_degrades_without_crash(monkeypatch, tmp_path, config):
    """Sin ANTHROPIC_API_KEY el SDK lanza TypeError al construir la petición: debe degradar, no romper."""
    monkeypatch.setattr(pipeline_mod, "YFinanceProvider", FakePrices)
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # sin perfiles de `ant auth login`
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    settings = Settings(openai_api_key=None, tavily_api_key=None, openai_model=None, project_root=tmp_path)
    record = asyncio.run(Pipeline(config, settings, RunOptions(mode="no-web"), "t4").run())
    assert record.total_extra_eur == 0
    assert any("credenciales de Anthropic" in e for e in record.errors)


def test_handoff_flow_resumes_stage_by_stage(monkeypatch, tmp_path, config, macro):
    monkeypatch.setattr(pipeline_mod, "YFinanceProvider", FakePrices)
    settings = Settings(openai_api_key=None, tavily_api_key=None, openai_model=None, project_root=tmp_path)
    handoff = tmp_path / "handoff"

    def step():
        p = Pipeline(config, settings, RunOptions(mode="no-web", handoff_dir=handoff), "h1")
        return p, asyncio.run(p.run())

    p, _ = step()
    assert p.pending == ["macro"] and (handoff / "requests" / "macro.md").exists()
    (handoff / "responses" / "macro.json").write_text(macro.model_dump_json(), encoding="utf-8")

    p, _ = step()
    assert sorted(p.pending) == ["gold", "msci_world"]
    (handoff / "responses" / "gold.json").write_text("{\"asset_id\": \"gold\"}", encoding="utf-8")  # inválido
    (handoff / "responses" / "msci_world.json").write_text(make_analysis("msci_world").model_dump_json(), encoding="utf-8")
    p, _ = step()
    assert p.pending == ["gold"] and (handoff / "requests" / "gold.error.txt").exists()
    (handoff / "responses" / "gold.json").write_text(make_analysis("gold", opportunity_score=3).model_dump_json(), encoding="utf-8")

    p, _ = step()
    assert p.pending == ["supervisor"]
    (handoff / "responses" / "supervisor.json").write_text(SupervisorOutput(
        executive_summary="ok", market_regime_summary="ok", overall_risk="medium", key_takeaways=[], best_opportunities=["msci_world"],
        avoid_extra=[], contradictions=[], recommendations=[], execution_plan="plan").model_dump_json(), encoding="utf-8")

    p, record = step()
    assert not p.pending and record.run_id == "h1"
    assert {d.asset_id: d.final_action for d in record.decisions}["gold"] == "no_extra"


def test_handoff_with_profiles_shares_analysis_and_splits_reports(monkeypatch, tmp_path, config, macro):
    from src.analysis.handoff_runner import handoff_status

    monkeypatch.setattr(pipeline_mod, "YFinanceProvider", FakePrices)
    settings = Settings(openai_api_key=None, tavily_api_key=None, openai_model=None, project_root=tmp_path)
    handoff = tmp_path / "handoff"
    profiles = {"todo": ["msci_world", "gold"], "oro": ["gold"]}
    supervisor = SupervisorOutput(
        executive_summary="ok", market_regime_summary="ok", overall_risk="medium", key_takeaways=[], best_opportunities=[],
        avoid_extra=[], contradictions=[], recommendations=[], execution_plan="plan")

    def step():
        p = Pipeline(config, settings, RunOptions(mode="no-web", handoff_dir=handoff, profiles=profiles), "hp")
        return p, asyncio.run(p.run_all())

    p, _ = step()
    assert handoff_status(handoff)["waiting_for"] == ["macro"]
    (handoff / "responses" / "macro.json").write_text(macro.model_dump_json(), encoding="utf-8")
    step()
    (handoff / "responses" / "gold.json").write_text(make_analysis("msci_world").model_dump_json(), encoding="utf-8")  # id equivocado
    (handoff / "responses" / "msci_world.json").write_text(make_analysis("msci_world").model_dump_json(), encoding="utf-8")
    status = handoff_status(handoff)
    assert status["waiting_for"] == ["gold"] and status["agents"]["gold"]["state"] == "invalid"
    (handoff / "responses" / "gold.json").write_text(make_analysis("gold").model_dump_json(), encoding="utf-8")

    p, _ = step()
    assert sorted(p.pending) == ["supervisor_oro", "supervisor_todo"]  # un supervisor por perfil, activos analizados una vez
    for label in p.pending:
        (handoff / "responses" / f"{label}.json").write_text(supervisor.model_dump_json(), encoding="utf-8")

    p, records = step()
    assert not p.pending and not handoff_status(handoff)["waiting_for"]
    by_profile = {r.profile: r for r in records}
    assert by_profile["oro"].run_id == "hp-oro" and by_profile["oro"].assets_requested == ["gold"]
    assert [d.asset_id for d in by_profile["oro"].decisions] == ["gold"]
    assert {d.asset_id for d in by_profile["todo"].decisions} == {"msci_world", "gold"}
    assert all(r.total_extra_eur <= config.rules.tactical_budget_eur for r in records)


def test_research_prefers_domains_and_widens_weak_searches(monkeypatch, tmp_path, minimal_config_dict):
    from types import SimpleNamespace

    from src.config import parse_config

    calls = []

    class DomainSearch(FakeSearch):
        def search(self, query, *, max_results, time_range, depth, include_domains=None):
            calls.append(include_domains)
            if include_domains == ["gold.org"]:
                return []  # nada en la web preferida del oro
            return super().search(query, max_results=max_results, time_range=time_range, depth=depth)

    minimal_config_dict["research"] = {"preferred_domains": {"equity": ["morningstar.com"], "commodity": ["reuters.com"]}, "min_preferred_results": 1}
    minimal_config_dict["assets"][1]["preferred_domains"] = ["gold.org"]  # el activo sustituye a su clase
    config = parse_config(minimal_config_dict)
    monkeypatch.setattr(pipeline_mod, "TavilyProvider", DomainSearch)
    settings = Settings(openai_api_key=None, tavily_api_key="y", openai_model=None, project_root=tmp_path)
    p = Pipeline(config, settings, RunOptions(skip_macro=True), "d1")
    p.stage_research({a.id: SimpleNamespace(prices=True) for a in config.assets})

    assert calls == [["morningstar.com"], ["gold.org"], None]  # la búsqueda abierta va después de las preferidas
    assert {s.scope for s in p.record.sources} == {"msci_world", "gold"}

    # Sin presupuesto para la ampliación, las búsquedas preferidas no se sacrifican
    calls.clear()
    config.research.max_searches_per_run = 2
    p = Pipeline(config, settings, RunOptions(skip_macro=True, use_cache=False), "d2")
    p.stage_research({a.id: SimpleNamespace(prices=True) for a in config.assets})
    assert calls == [["morningstar.com"], ["gold.org"]] and p.record.search_usage.quota_exhausted


def test_unknown_domain_scope_rejected(minimal_config_dict):
    from src.config import parse_config
    from src.core.errors import ConfigError

    minimal_config_dict["research"] = {"preferred_domains": {"acciones": ["x.com"]}}
    with pytest.raises(ConfigError, match="ámbitos desconocidos"):
        parse_config(minimal_config_dict)
