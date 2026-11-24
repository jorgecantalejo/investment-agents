"""Web pública: elección del último análisis, cambios frente al anterior, páginas y ausencia de importes privados."""

from pathlib import Path

from src.models import FactorStudy, MacroFactor, Source
from src.quant.trend_snapshot import build_trend_strategy
from src.reporting.site import build_site, changes_vs_previous, pick_latest, render_site, site_context, write_site
from src.storage.run_store import RunStore
from tests.test_trend import AFTER_CLOSE, FakeService, ohlc_from_close, strategy_cfg

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
CAPITAL = 98765  # importe distintivo: nunca debe aparecer en la web


def _gold_strategy():
    up = [100.0] * 25 + [100 + i for i in range(1, 15)]
    cfg = strategy_cfg(capital_eur=CAPITAL)
    return build_trend_strategy("gold_trend", cfg, FakeService(ohlc_from_close(up, spread=0.2)), now=AFTER_CLOSE)


def _factors():
    return FactorStudy(asset_name="Oro", price_basis="test", since="2004-01", until="2026-11-20", months=200, base_avg_3m_pct=2.0,
                       base_up_share_3m_pct=60, unfavorable=1, method=["m"],
                       factors=[MacroFactor(id="r", name="Tipo real", source="FRED (DFII10)", unit="%", change_unit="pb",
                                            usual="Tipos al alza → oro a la baja.", usual_sign=-1, value=2.5, change_3m=40,
                                            direction="up", signal="unfavorable", edge_3m_pp=-2.1, t_stat=-2.3, observed_sign=1),
                                MacroFactor(id="x", name="Roto", source="Yahoo", change_unit="%", usual="u", usual_sign=1, error="sin datos")])


def _with_gold(record, created_at, score, action, run_id):
    gold_snap = record.snapshots[0].model_copy(update={"asset_id": "gold", "name": "Oro", "ticker": "PPFB.DE"})
    gold_dec = record.decisions[0].model_copy(update={"asset_id": "gold", "name": "Oro", "opportunity_score": score,
                                                      "final_action": action, "amount_eur": 4321})
    return record.model_copy(update={
        "run_id": run_id, "created_at": created_at, "snapshots": record.snapshots + [gold_snap],
        "decisions": record.decisions + [gold_dec],
        "asset_analyses": {**record.asset_analyses, "gold": record.asset_analyses["msci_world"].model_copy(update={"asset_id": "gold"})},
        "sources": record.sources + [Source(id="S2", title="Oro", url="https://example.com/oro", retrieved_at="2026-11-16", query="q", scope="gold")],
    })


def test_pick_latest_and_changes(run_record):
    old = _with_gold(run_record, "2026-11-09T10:00:00", 4, "no_extra", "old")
    new = _with_gold(run_record, "2026-11-16T10:00:00", 6, "small_extra", "new")
    quant = new.model_copy(update={"run_id": "quant", "created_at": "2026-11-20T10:00:00", "macro": None, "asset_analyses": {}})
    records = [quant, new, old]
    latest = pick_latest(records)
    assert latest.run_id == "new"
    changes, prev_date = changes_vs_previous(latest, records)
    assert prev_date == "2026-11-09"
    assert changes["gold"] == {"date": "2026-11-09", "score_from": 4, "score_to": 6, "action_from": "no_extra",
                               "action_to": "small_extra", "direction": "up"}


def test_pages_render_without_private_amounts(run_record):
    old = _with_gold(run_record, "2026-11-09T10:00:00", 4, "no_extra", "old")
    new = _with_gold(run_record, "2026-11-16T10:00:00", 6, "small_extra", "new")
    ctx = site_context(new, [new, old], _gold_strategy(), _factors(), "2026-11-16T18:00:00")
    pages = render_site(ctx, TEMPLATES)
    index, oro = pages["index.html"], pages["oro.html"]

    assert "Régimen macroeconómico" in index and 'href="oro.html"' in index and "4→6" in index
    assert "Seguimiento de tendencia" in oro and 'id="factores"' in oro and "Perjudica al oro" in oro
    assert "No disponible: sin datos" in oro and "Los datos muestran lo contrario" in oro
    for html in pages.values():
        for private in ("98765", "98 765", "4321", "4 321", "190 EUR", "Capital asignado", "participaciones", "Presupuesto de riesgo"):
            assert private not in html, private


def test_index_without_analysis_and_stale_pages_removed(tmp_path):
    (tmp_path / "informe_2026-11-01.html").write_text("viejo", encoding="utf-8")
    paths = build_site(tmp_path / "runs", TEMPLATES, tmp_path, _gold_strategy(), None)
    assert sorted(p.name for p in paths) == ["index.html", "oro.html", "sp500.html"]
    assert not (tmp_path / "informe_2026-11-01.html").exists() and (tmp_path / ".nojekyll").exists()
    assert "Todavía no hay ningún análisis semanal" in (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "Estudio de factores no disponible" in (tmp_path / "oro.html").read_text(encoding="utf-8")


def test_build_site_reads_saved_runs(run_record, tmp_path):
    store = RunStore(tmp_path / "runs")
    store.save(_with_gold(run_record, "2026-11-16T10:00:00", 6, "small_extra", "20261116-100000-aaaaaa"))
    write_site({"index.html": "x"}, tmp_path / "site")  # una web previa se sobrescribe
    build_site(tmp_path / "runs", TEMPLATES, tmp_path / "site", None, _factors())
    oro = (tmp_path / "site" / "oro.html").read_text(encoding="utf-8")
    assert "Análisis <b>2026-11-16</b>" in oro and "No hay ninguna estrategia de tendencia" in oro
