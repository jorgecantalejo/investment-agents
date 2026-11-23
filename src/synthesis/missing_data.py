"""Recopila las limitaciones del análisis para la sección «Datos faltantes»."""

from __future__ import annotations

from src.analysis.schemas import AssetAnalysis, MacroAnalysis
from src.models import PERIODS, AssetSnapshot, MissingDataItem, SearchUsage, Source

_VALUATION_LABELS = {
    "trailing_pe": "PER trailing",
    "forward_pe": "PER forward",
    "cape": "CAPE / Shiller PE",
    "ev_ebitda": "EV/EBITDA",
    "price_to_sales": "Price/Sales",
    "dividend_yield_pct": "Dividend yield",
}


def collect(
    snapshots: list[AssetSnapshot],
    analyses: dict[str, AssetAnalysis],
    macro: MacroAnalysis | None,
    sources: list[Source],
    usage: SearchUsage,
    mode: str,
    llm_errors: dict[str, str],
) -> list[MissingDataItem]:
    items: list[MissingDataItem] = []
    if mode == "quant":
        items.append(MissingDataItem(scope="global", item="Modo cuantitativo: sin búsqueda web ni análisis LLM", impact="No se recomienda entrar en ningún activo"))
    if usage.disabled_reason and mode == "full":
        items.append(MissingDataItem(scope="global", item=f"Búsqueda web no disponible: {usage.disabled_reason}", impact="Sentimiento y contexto sin fuentes; convicción reducida"))
    if usage.quota_exhausted:
        items.append(MissingDataItem(scope="global", item="Presupuesto o créditos de búsqueda agotados durante la ejecución", impact="Algunos activos sin noticias recientes"))
    if mode != "quant" and macro is None:
        items.append(MissingDataItem(scope="macro", item=f"Análisis macro no disponible{': ' + llm_errors['macro'] if 'macro' in llm_errors else ''}", impact="Entradas limitadas a «entrada pequeña»"))
    if macro:
        items += [MissingDataItem(scope="macro", item=g) for g in macro.data_gaps]
        if not macro.upcoming_events:
            items.append(MissingDataItem(scope="macro", item="Sin próximos eventos verificados con fuente", impact="Consultar calendario macro manualmente"))
    undated = [s.id for s in sources if not s.published_date]
    if undated:
        items.append(MissingDataItem(scope="global", item=f"Fuentes sin fecha de publicación: {', '.join(undated)}", impact="Verificar vigencia antes de decidir"))

    for snap in snapshots:
        if snap.prices is None:
            items.append(MissingDataItem(scope=snap.asset_id, item=f"Sin datos de precio: {snap.error}", impact="Activo excluido de recomendaciones"))
            continue
        items += [MissingDataItem(scope=snap.asset_id, item=issue) for issue in snap.quality.issues]
        p = snap.prices
        for label, value in (("SMA 50", p.sma50), ("SMA 200", p.sma200), ("Volatilidad", p.volatility_annualized_pct)):
            if value is None:
                items.append(MissingDataItem(scope=snap.asset_id, item=f"{label}: insufficient_data"))
        if p.benchmark_ticker and all(p.relative_vs_benchmark_pp.get(k) is None for k in PERIODS):
            items.append(MissingDataItem(scope=snap.asset_id, item="Comparativa vs benchmark: insufficient_data"))
        if snap.valuation:
            missing = [lbl for f, lbl in _VALUATION_LABELS.items() if getattr(snap.valuation, f) is None]
            if missing:
                items.append(MissingDataItem(scope=snap.asset_id, item=f"Valuación sin dato: {', '.join(missing)}", impact="Juicio de valuación menos fiable"))
            if all(v is None for v in snap.valuation.percentiles.values()):
                items.append(MissingDataItem(scope=snap.asset_id, item="Percentiles históricos de valuación: insufficient_data (la fuente no ofrece histórico)"))
        if mode == "full" and not any(s.scope == snap.asset_id for s in sources):
            items.append(MissingDataItem(scope=snap.asset_id, item="Sin fuentes web recientes", impact="Sentimiento no verificable"))
        if mode != "quant" and snap.asset_id not in analyses:
            items.append(MissingDataItem(scope=snap.asset_id, item=f"Análisis LLM no disponible{': ' + llm_errors[snap.asset_id] if snap.asset_id in llm_errors else ''}", impact="No se recomienda entrar"))
        if snap.asset_id in analyses:
            items += [MissingDataItem(scope=snap.asset_id, item=m) for m in analyses[snap.asset_id].missing_data]
    return items
