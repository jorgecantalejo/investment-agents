"""Construye el `AssetSnapshot` de cada activo combinando datos de mercado y cálculos."""

from __future__ import annotations

import logging
from datetime import date

from src.config.models import AppConfig, AssetConfig
from src.core.errors import MarketDataError
from src.market.service import MarketDataService
from src.models import AssetSnapshot, CapeStats, DataQuality, PriceMetrics, ValuationMetrics
from src.quant import metrics

logger = logging.getLogger(__name__)

VALUATION_FIELDS = ("trailing_pe", "forward_pe", "cape", "ev_ebitda", "price_to_sales", "dividend_yield_pct")


def _price_metrics(asset: AssetConfig, config: AppConfig, service: MarketDataService) -> tuple[PriceMetrics, bool, list[str]]:
    report_ccy = config.report.currency
    issues: list[str] = []
    local = service.get_close(asset.ticker)
    close = local.close
    stale = local.from_stale_cache

    in_report_ccy = close
    if config.data.convert_to_report_currency and asset.currency != report_ccy:
        try:
            converted = service.get_close_in_currency(asset.ticker, asset.currency, report_ccy)
            in_report_ccy = converted.close
            stale = stale or converted.from_stale_cache
        except MarketDataError as exc:
            issues.append(f"No se pudo convertir a {report_ccy} ({exc}); rentabilidades solo en {asset.currency}")
            in_report_ccy = None

    price = float(close.iloc[-1])
    sma50 = metrics.simple_moving_average(close, 50)
    sma200 = metrics.simple_moving_average(close, 200)
    returns_local = metrics.returns_by_period(close)
    returns_rc = metrics.returns_by_period(in_report_ccy) if in_report_ccy is not None else dict.fromkeys(metrics.PERIOD_MONTHS)
    dd, high, high_date = metrics.drawdown_from_high(close, config.data.recent_high_window_days)

    benchmark = config.benchmark_for(asset)
    bench_returns: dict[str, float | None] = dict.fromkeys(metrics.PERIOD_MONTHS)
    if benchmark and benchmark != asset.ticker:
        bench_ccy = next((a.currency for a in config.assets if a.ticker == benchmark), None)
        try:
            if bench_ccy and config.data.convert_to_report_currency:
                bench_close = service.get_close_in_currency(benchmark, bench_ccy, report_ccy).close
            else:
                bench_close = service.get_close(benchmark).close
                if bench_ccy is None:
                    issues.append(f"Benchmark {benchmark} no está en el universo: comparativa sin ajuste de divisa")
            bench_returns = metrics.returns_by_period(bench_close)
        except MarketDataError as exc:
            issues.append(f"Benchmark {benchmark} no disponible: {exc}")

    return (
        PriceMetrics(
            currency=asset.currency,
            last_price=round(price, 4),
            last_date=close.index[-1].strftime("%Y-%m-%d"),
            observations=len(close),
            returns_pct=returns_local,
            returns_report_ccy_pct=returns_rc,
            report_currency=report_ccy,
            sma50=sma50,
            sma200=sma200,
            above_sma50=price > sma50 if sma50 is not None else None,
            above_sma200=price > sma200 if sma200 is not None else None,
            recent_high=high,
            recent_high_date=high_date,
            drawdown_from_recent_high_pct=dd,
            max_drawdown_window_pct=metrics.max_drawdown(close, config.data.recent_high_window_days),
            volatility_annualized_pct=metrics.annualized_volatility(close),
            benchmark_ticker=benchmark if benchmark != asset.ticker else None,
            benchmark_returns_report_ccy_pct=bench_returns,
            relative_vs_benchmark_pp=metrics.relative_performance(returns_rc, bench_returns),
            quant_trend=metrics.classify_trend(price, sma50, sma200, returns_local["6m"]),  # type: ignore[arg-type]
            sparkline=metrics.weekly_closes(close, 365),
        ),
        stale,
        issues,
    )


def _assess_quality(prices: PriceMetrics, config: AppConfig, issues: list[str], stale: bool) -> DataQuality:
    age_days = (date.today() - date.fromisoformat(prices.last_date)).days
    if stale:
        issues.append("Datos de precio procedentes de caché caducada (proveedor no disponible)")
    if age_days > config.data.stale_after_days:
        issues.append(f"Último dato de precio con {age_days} días de antigüedad")
    if prices.observations < config.data.min_observations:
        issues.append(f"Solo {prices.observations} observaciones (mínimo {config.data.min_observations})")
    missing_core = [k for k, v in prices.returns_pct.items() if v is None]
    if missing_core:
        issues.append(f"Rentabilidades no calculables: {', '.join(missing_core)}")

    if prices.observations < config.data.min_observations or age_days > 3 * config.data.stale_after_days:
        level = "poor"
    elif issues:
        level = "partial"
    else:
        level = "good"
    return DataQuality(level=level, issues=issues)  # type: ignore[arg-type]


def build_snapshot(asset: AssetConfig, config: AppConfig, service: MarketDataService, cape_data: CapeStats | None = None) -> AssetSnapshot:
    """Snapshot de un activo. Nunca lanza: los fallos quedan registrados en `quality`/`error`."""
    base = dict(asset_id=asset.id, name=asset.name, ticker=asset.ticker, asset_class=asset.asset_class)
    try:
        prices, stale, issues = _price_metrics(asset, config, service)
    except MarketDataError as exc:
        logger.error("Sin datos de mercado para %s (%s): %s", asset.id, asset.ticker, exc)
        return AssetSnapshot(**base, quality=DataQuality(level="unavailable", issues=[str(exc)]), error=str(exc))
    except Exception as exc:  # cálculo inesperado: no tumbar la ejecución entera
        logger.exception("Error calculando métricas de %s", asset.id)
        return AssetSnapshot(**base, quality=DataQuality(level="unavailable", issues=[f"Error interno: {exc}"]), error=str(exc))

    valuation: ValuationMetrics | None = None
    if asset.asset_class == "equity":
        source = asset.valuation_proxy_ticker or asset.ticker
        valuation = service.get_valuation(source)
        valuation.percentiles = dict.fromkeys(VALUATION_FIELDS)  # solo el CAPE tiene histórico
        if asset.cape_reference == "sp500":
            if cape_data:
                valuation.cape, valuation.cape_source = cape_data.value, f"S&P 500 a {cape_data.as_of}, {cape_data.source}"
                valuation.percentiles.update(cape=cape_data.percentile_all, cape_30y=cape_data.percentile_30y)
            else:
                issues.append("CAPE del S&P 500 no disponible (fuente sin respuesta)")
        if all(getattr(valuation, f) is None for f in VALUATION_FIELDS):
            issues.append(f"Sin métricas de valuación en {valuation.provider} para {source}")

    quality = _assess_quality(prices, config, issues, stale)
    return AssetSnapshot(**base, prices=prices, valuation=valuation, quality=quality, used_stale_cache=stale)
