"""Tests de generación del informe HTML."""

from pathlib import Path

from src.reporting.html import render_report, write_report

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
RULES = {"tactical_budget_eur": 300}


def test_render_contains_required_sections(run_record):
    html = render_report(run_record, TEMPLATES, "Informe test", RULES)
    for text in (
        "Informe test", "2026-11-16", "EUR", "Resumen ejecutivo", "Régimen macroeconómico", "Próximos eventos relevantes",
        "Comparativa de activos", "Fichas por activo", "Señales contradictorias", "Datos faltantes",
        "Tabla final de recomendaciones", "Fuentes", "No constituye asesoramiento financiero personalizado",
        "https://example.com/fed", "Reunión de la Fed", "score-high", "190",
    ):
        assert text in html, text


def test_missing_metrics_shown_as_insufficient_data(run_record):
    html = render_report(run_record, TEMPLATES, "t", RULES)
    assert "insufficient_data" in html  # forward PE, CAPE...
    assert "Sin datos para" in html     # activo sin precios


def test_html_is_escaped(run_record):
    html = render_report(run_record, TEMPLATES, "t", RULES)
    assert "Fed <decision>" not in html and "Fed &lt;decision&gt;" in html


def test_render_without_llm_outputs(run_record):
    record = run_record.model_copy(update={"macro": None, "supervisor": None, "asset_analyses": {}, "decisions": [], "sources": []})
    html = render_report(record, TEMPLATES, "t", RULES)
    assert "Análisis macro no disponible" in html
    assert "No se propone ninguna entrada" in html


def test_write_report_falls_back_when_template_broken(run_record, tmp_path):
    (tmp_path / "report.html").write_text("{{ undefined_variable.foo }}", encoding="utf-8")
    path, fallback = write_report(run_record, tmp_path, tmp_path / "out" / "informe.html", "t", RULES)
    assert fallback and path.exists()
    assert "Informe de emergencia" in path.read_text(encoding="utf-8")


def test_comparison_table_header_matches_row_cells(run_record):
    """Cada fila de la tabla comparativa debe tener tantas celdas como columnas la cabecera."""
    import re
    html = render_report(run_record, TEMPLATES, "t", RULES)
    table = html.split("Comparativa de activos", 1)[1].split("</table>", 1)[0]
    rows = re.findall(r"<tr>(.*?)</tr>", table, re.S)
    header_cols = len(re.findall(r"<th", rows[0]))
    first_row = rows[1]
    cells = sum(int(m or 1) for m in re.findall(r"<td(?:[^>]*colspan=\"(\d+)\")?", first_row))
    assert cells == header_cols


def test_header_indicators_and_charts(run_record):
    from src.models import MarketIndicator
    record = run_record.model_copy(update={"indicators": [
        MarketIndicator(id="fear_greed_stocks", name="Miedo y codicia · acciones", value=31.0, kind="sentiment", previous={"1w": 31.5, "1m": 18.2}, source="CNN"),
        MarketIndicator(id="fear_greed_crypto", name="Miedo y codicia · cripto", value=None, kind="sentiment", source="alternative.me"),
        MarketIndicator(id="vix", name="VIX", value=22.9, kind="volatility", previous={"1m": 18.0}),
    ]})
    html = render_report(record, TEMPLATES, "t", RULES)
    assert 'class="gauge"' in html and "Miedo" in html           # medidor con etiqueta textual
    assert "+4.90 pts" in html                                     # delta del VIX calculado
    assert 'class="spark spark-' in html or "sin histórico" in html
    assert 'href="#asset-msci_world"' in html and 'id="asset-msci_world"' in html  # tarjeta resumen -> ficha
    assert "aportación mensual" not in html.lower()


def test_fear_greed_bands():
    from src.reporting.charts import fear_greed_label
    assert fear_greed_label(10) == "Miedo extremo"
    assert fear_greed_label(50) == "Neutral"
    assert fear_greed_label(90) == "Codicia extrema"
    assert fear_greed_label(None) == "insufficient_data"
