"""Render del informe HTML con Jinja2 y un informe mínimo de emergencia si la plantilla falla."""

from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError, select_autoescape

from src.core.errors import ReportError
from src.models import RunRecord, TrendStrategyResult
from src.reporting import charts
from src.reporting.builder import ACTION_LABELS, LEVEL_LABELS, LEVEL_LABELS_F, TERM_LABELS, TREND_STATE_LABELS, build_context

logger = logging.getLogger(__name__)


def _pct(value: float | None, signed: bool = True) -> str:
    if value is None:
        return "insufficient_data"
    if value == 0:
        return "0.00%"
    return f"{value:+.2f}%" if signed else f"{value:.2f}%"


def _num(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "insufficient_data"
    return f"{value:,.{digits}f}".replace(",", " ")


def _sign_class(value: float | None) -> str:
    if value is None:
        return "na"
    return "pos" if value > 0 else "neg" if value < 0 else ""


def _score_class(score: int | None) -> str:
    if score is None:
        return "score-na"
    return "score-high" if score >= 7 else "score-mid" if score >= 4 else "score-low"


def make_env(templates_dir: Path) -> Environment:
    env = Environment(
        loader=FileSystemLoader(templates_dir),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        pct=_pct,
        num=_num,
        sign_class=_sign_class,
        score_class=_score_class,
        action_label=lambda a: ACTION_LABELS.get(a, a or "—"),
        level_label=lambda v: LEVEL_LABELS.get(v, v or "—"),
        level_label_f=lambda v: LEVEL_LABELS_F.get(v, v or "—"),
        term=lambda v: TERM_LABELS.get(v, v or "—"),
        yesno=lambda v: "insufficient_data" if v is None else ("Sí" if v else "No"),
        trend_state=lambda v: TREND_STATE_LABELS.get(v, v or "—"),
    )
    env.globals.update(gauge=charts.gauge, sparkline=charts.sparkline, score_ring=charts.score_ring, fg_label=charts.fear_greed_label,
                       trend_chart=charts.trend_chart, r_bars=charts.r_bars, regime_chart=charts.regime_chart)
    return env


def render_report(record: RunRecord, templates_dir: Path, title: str, rules: dict[str, Any]) -> str:
    try:
        template = make_env(templates_dir).get_template("report.html")
        return template.render(**build_context(record, title, rules))
    except TemplateError as exc:
        raise ReportError(f"Error en la plantilla HTML: {exc}") from exc
    except Exception as exc:
        raise ReportError(f"Error preparando el informe: {type(exc).__name__}: {exc}") from exc


def write_strategy_report(strategies: list[TrendStrategyResult], templates_dir: Path, output_path: Path, title: str, generated_at: str) -> Path:
    """Informe propio de las estrategias de tendencia (sin LLM). Un fallo de plantilla se propaga como ReportError."""
    try:
        content = make_env(templates_dir).get_template("strategy.html").render(
            strategies=strategies, title=title, date=generated_at[:10], generated_at=generated_at)
    except Exception as exc:
        raise ReportError(f"Error en la plantilla de estrategias: {type(exc).__name__}: {exc}") from exc
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return output_path


def render_fallback(record: RunRecord, title: str, reason: str) -> str:
    """HTML mínimo sin plantilla para no quedarse sin informe."""
    rows = "".join(
        f"<tr><td>{html.escape(d.name)}</td><td>{d.opportunity_score if d.opportunity_score is not None else '—'}</td>"
        f"<td>{ACTION_LABELS[d.final_action]}</td><td>{d.amount_eur:.0f} EUR</td></tr>"
        for d in record.decisions
    )
    return (
        f"<!DOCTYPE html><html lang='es'><meta charset='utf-8'><title>{html.escape(title)}</title>"
        f"<body style='font-family:sans-serif;max-width:800px;margin:auto;padding:16px'>"
        f"<h1>{html.escape(title)} — {record.created_at[:10]}</h1>"
        f"<p style='color:#b02a37'><b>Informe de emergencia:</b> la plantilla falló ({html.escape(reason)}). "
        f"El detalle completo está en el JSON de la ejecución {record.run_id}.</p>"
        f"<table border='1' cellpadding='6'><tr><th>Activo</th><th>Puntuación</th><th>Acción</th><th>Importe</th></tr>{rows}</table>"
        f"<p>No es asesoramiento financiero personalizado.</p></body></html>"
    )


def write_report(record: RunRecord, templates_dir: Path, output_path: Path, title: str, rules: dict[str, Any]) -> tuple[Path, bool]:
    """Escribe el informe. Devuelve (ruta, ¿se usó el fallback?)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    used_fallback = False
    try:
        content = render_report(record, templates_dir, title, rules)
    except ReportError as exc:
        logger.error("%s. Se genera informe de emergencia.", exc)
        content = render_fallback(record, title, str(exc))
        used_fallback = True
    output_path.write_text(content, encoding="utf-8")
    return output_path, used_fallback
