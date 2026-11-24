"""Web pública de análisis (GitHub Pages): portada macro y páginas del oro y del S&P 500.

Sin histórico: cada generación sobrescribe las mismas URLs. Combina:
- el último análisis semanal completo guardado en data/runs (macro, activos, supervisor),
- la estrategia de tendencia del oro, el estudio de factores y la estrategia de régimen del S&P 500, recalculados al generar.

La web es pública: el contexto de las plantillas no incluye capital, presupuestos ni importes en euros.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.errors import ReportError
from src.models import FactorStudy, RegimeStrategyResult, RunRecord, TrendStrategyResult
from src.reporting.builder import build_context
from src.reporting.html import make_env
from src.storage.run_store import RunStore

logger = logging.getLogger(__name__)

GOLD_ASSET_ID = "gold"
SP500_ASSET_ID = "sp500"
PAGES = {GOLD_ASSET_ID: "oro.html", SP500_ASSET_ID: "sp500.html"}  # activos con página propia; el resto se irá añadiendo
NAV = [("index.html", "Mercado"), ("sp500.html", "S&P 500"), ("oro.html", "Oro")]
TEMPLATES = {"index.html": "site/index.html", "oro.html": "site/oro.html", "sp500.html": "site/sp500.html"}
MAX_RUNS_SCANNED = 60

DIRECTION_LABELS = {"up": "Sube", "down": "Baja", "flat": "Estable", "insufficient_data": "insufficient_data"}
SIGNAL_LABELS = {"favorable": "Favorece al oro", "unfavorable": "Perjudica al oro", "neutral": "Sin efecto claro",
                 "insufficient_data": "Datos insuficientes"}


def _factor_change(value: float | None, unit: str) -> str:
    if value is None:
        return "insufficient_data"
    if unit == "pb":
        return f"{value:+.0f} pb"
    return f"{value:+.2f}%" if unit == "%" else f"{value:+.2f} pts"


def is_full_analysis(record: RunRecord) -> bool:
    return record.macro is not None and bool(record.asset_analyses)


def load_runs(runs_dir: Path, limit: int = MAX_RUNS_SCANNED) -> list[RunRecord]:
    """Ejecuciones guardadas, de la más reciente a la más antigua (solo las `limit` últimas)."""
    records = []
    for path in sorted(runs_dir.glob("run_*.json"), reverse=True)[:limit]:
        try:
            records.append(RunStore.load(path))
        except (OSError, ValueError) as exc:
            logger.warning("No se pudo leer %s: %s", path.name, exc)
    return sorted(records, key=lambda r: r.created_at, reverse=True)


def pick_latest(records: list[RunRecord]) -> RunRecord | None:
    """Último análisis completo; entre los de la misma ejecución (varios perfiles), el que cubre más activos."""
    full = [r for r in records if is_full_analysis(r)]
    if not full:
        return None
    latest_date = full[0].created_at
    same_run = [r for r in full if r.created_at == latest_date]
    return max(same_run, key=lambda r: len(r.snapshots))


def changes_vs_previous(latest: RunRecord, records: list[RunRecord]) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Para cada activo, puntuación y acción del último análisis completo anterior (de otro día) que lo incluya."""
    current = {d.asset_id: d for d in latest.decisions}
    changes: dict[str, dict[str, Any]] = {}
    dates = set()
    for asset_id, now in current.items():
        for record in records:
            if record.created_at[:10] >= latest.created_at[:10] or not is_full_analysis(record):
                continue
            prev = next((d for d in record.decisions if d.asset_id == asset_id), None)
            if prev is None:
                continue
            score_from, score_to = prev.opportunity_score, now.opportunity_score
            direction = ""
            if score_from is not None and score_to is not None and score_from != score_to:
                direction = "up" if score_to > score_from else "down"
            changes[asset_id] = {"date": record.created_at[:10], "score_from": score_from, "score_to": score_to,
                                 "action_from": prev.final_action, "action_to": now.final_action, "direction": direction}
            dates.add(record.created_at[:10])
            break
    return changes, (max(dates) if dates else None)


def site_context(latest: RunRecord | None, records: list[RunRecord], gold_strategy: TrendStrategyResult | None,
                 factors: FactorStudy | None, generated_at: str, sp500_regime: RegimeStrategyResult | None = None) -> dict[str, Any]:
    """Contexto común de las páginas. Reutiliza el del informe pero sin capital ni importes."""
    ctx: dict[str, Any] = {"run": None, "date": None, "cards": [], "macro": None, "supervisor": None, "indicators": {},
                           "best": [], "avoid": [], "macro_sources": [], "source_map": {}, "currency": "EUR"}
    if latest is not None:
        full = build_context(latest, "Análisis de mercado", {})
        ctx.update({k: full[k] for k in ("run", "date", "cards", "macro", "supervisor", "indicators", "best", "avoid",
                                          "macro_sources", "source_map", "currency")})
    changes, previous_date = changes_vs_previous(latest, records) if latest else ({}, None)
    asset_cards = {c["snapshot"].asset_id: c for c in ctx["cards"]}
    ctx.update(changes=changes, previous_date=previous_date, pages=PAGES, nav=NAV, generated_at=generated_at, asset_cards=asset_cards,
               gold_card=asset_cards.get(GOLD_ASSET_ID), change=changes.get(GOLD_ASSET_ID), gold_strategy=gold_strategy, factors=factors,
               sp500_regime=sp500_regime)
    return ctx


def render_site(ctx: dict[str, Any], templates_dir: Path) -> dict[str, str]:
    env = make_env(templates_dir)
    env.filters.update(factor_change=_factor_change, factor_direction=lambda v: DIRECTION_LABELS.get(v, v),
                       factor_signal=lambda v: SIGNAL_LABELS.get(v, v))
    pages = {}
    for name, template in TEMPLATES.items():
        try:
            pages[name] = env.get_template(template).render(**ctx, page=name)
        except Exception as exc:
            raise ReportError(f"Error en la plantilla {template}: {type(exc).__name__}: {exc}") from exc
    return pages


def write_site(pages: dict[str, str], out_dir: Path) -> list[Path]:
    """Escribe las páginas y borra los HTML que ya no forman parte de la web (sin histórico)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / ".nojekyll").touch()
    for stale in out_dir.glob("*.html"):
        if stale.name not in pages:
            stale.unlink()
    written = []
    for name, content in pages.items():
        path = out_dir / name
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def build_site(runs_dir: Path, templates_dir: Path, out_dir: Path, gold_strategy: TrendStrategyResult | None,
               factors: FactorStudy | None, now: datetime | None = None, sp500_regime: RegimeStrategyResult | None = None) -> list[Path]:
    records = load_runs(runs_dir)
    latest = pick_latest(records)
    if latest is None:
        logger.warning("No hay ningún análisis semanal completo en %s: la portada saldrá sin análisis", runs_dir)
    generated_at = (now or datetime.now()).isoformat(timespec="seconds")
    ctx = site_context(latest, records, gold_strategy, factors, generated_at, sp500_regime)
    return write_site(render_site(ctx, templates_dir), out_dir)
