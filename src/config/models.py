"""Esquema Pydantic de `config.yaml`. Valida universo de activos y reglas personales."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AssetClass = Literal["equity", "bond", "commodity", "crypto", "real_estate", "private_markets", "other"]
Action = Literal["no_extra", "small_extra", "normal_extra", "staged_extra"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReportConfig(_Strict):
    title: str = "Informe de inversión"
    currency: str = "EUR"
    language: str = "es"
    output_dir: str = "reports"


class TierFractions(_Strict):
    """Fracción de `max_single_buy_eur` asignada a cada nivel de recomendación."""

    small_extra: float = Field(0.33, gt=0, le=1)
    normal_extra: float = Field(0.66, gt=0, le=1)
    staged_extra: float = Field(1.0, gt=0, le=1)


class RulesConfig(_Strict):
    tactical_budget_eur: float = Field(300, gt=0, description="Capital de referencia que el informe reparte entre las entradas")
    max_single_buy_eur: float = Field(300, gt=0)
    max_assets_with_extra: int = Field(2, ge=1)
    min_score_for_extra: int = Field(6, ge=0, le=10)
    min_ticket_eur: float = Field(50, ge=0)
    rounding_eur: float = Field(10, gt=0)
    tier_fractions: TierFractions = TierFractions()
    staged_tranches: int = Field(3, ge=2, le=6)
    staged_interval_days: int = Field(14, ge=1)
    momentum_chase_return_12m_pct: float = Field(30.0, description="Rentabilidad 12m (EUR) a partir de la cual se exige valuación no cara")
    min_cash_buffer_months: int = Field(6, ge=0)
    horizon_years: int = Field(10, ge=1)

    @model_validator(mode="after")
    def _check_budget(self) -> RulesConfig:
        if self.max_single_buy_eur > self.tactical_budget_eur:
            raise ValueError("max_single_buy_eur no puede superar tactical_budget_eur")
        return self


class DataConfig(_Strict):
    history_period: str = "5y"
    recent_high_window_days: int = Field(365, ge=30, description="Ventana (días naturales) del máximo reciente para el drawdown")
    stale_after_days: int = Field(5, ge=1)
    min_observations: int = Field(200, ge=20)
    price_cache_ttl_hours: float = Field(12, ge=0)
    fundamentals_cache_ttl_hours: float = Field(24, ge=0)
    convert_to_report_currency: bool = True
    macro_data: bool = Field(True, description="Series macro de FRED y CAPE de Shiller (fuentes públicas sin clave)")
    macro_cache_ttl_hours: float = Field(24, ge=0)


class SearchQuery(_Strict):
    query: str
    time_range: Literal["day", "week", "month", "year"] = "week"


class ResearchConfig(_Strict):
    enabled: bool = True
    provider: Literal["tavily"] = "tavily"
    max_searches_per_run: int = Field(20, ge=0)
    results_per_query: int = Field(5, ge=1, le=20)
    search_depth: Literal["basic", "advanced"] = "basic"
    cache_ttl_hours: float = Field(12, ge=0)
    snippet_chars: int = Field(900, ge=100)
    macro_queries: list[SearchQuery] = Field(default_factory=list)
    events_queries: list[SearchQuery] = Field(default_factory=list)
    asset_query_template: str = "{search_terms} market outlook valuation sentiment"
    preferred_domains: dict[str, list[str]] = Field(
        default_factory=dict, description="Webs a las que se limita cada búsqueda, por ámbito: 'macro', 'events' o clase de activo")
    min_preferred_results: int = Field(2, ge=0, description="Con menos resultados en las webs preferidas se repite sin restricción (si queda presupuesto)")

    @field_validator("preferred_domains")
    @classmethod
    def _check_domain_scopes(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        valid = {"macro", "events", *AssetClass.__args__}
        unknown = sorted(set(value) - valid)
        if unknown:
            raise ValueError(f"ámbitos desconocidos en preferred_domains: {unknown}. Válidos: {sorted(valid)}")
        return value


class LLMConfig(_Strict):
    provider: Literal["anthropic", "openai"] = "anthropic"
    model: str | None = Field(None, description="None = modelo por defecto del proveedor (o variable LLM_MODEL)")
    effort: Literal["low", "medium", "high", "xhigh", "max"] = Field("medium", description="Solo Anthropic: profundidad de razonamiento")
    max_retries: int = Field(2, ge=0, le=5)
    timeout_seconds: float = Field(180, gt=0)
    max_concurrency: int = Field(3, ge=1, le=10)
    tracing: bool = Field(False, description="Solo OpenAI: enviar trazas a la plataforma de OpenAI")


class DefaultsConfig(_Strict):
    benchmark_ticker: str = "IWDA.AS"


class AssetConfig(_Strict):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    name: str
    ticker: str
    asset_class: AssetClass
    category: str = ""
    currency: str = Field("USD", pattern=r"^[A-Z]{3}$")
    benchmark_ticker: str | None = None
    valuation_proxy_ticker: str | None = Field(None, description="ETF del que leer PER/yield si el ticker es un índice")
    search_terms: str | None = None
    preferred_domains: list[str] | None = Field(None, description="Sustituye a research.preferred_domains[asset_class] para este activo")
    cape_reference: Literal["sp500"] | None = Field(None, description="Serie CAPE aplicable al activo (solo si replica ese índice)")
    enabled: bool = True
    notes: str = ""

    @property
    def search_label(self) -> str:
        return self.search_terms or self.name


class TrendStrategyConfig(_Strict):
    """Estrategia experimental de seguimiento de tendencia (filtro SMA + ruptura de máximos + stop ATR).

    Todo se calcula en Python sobre OHLC diario; no interviene el LLM. Los importes van en `currency`,
    que debe coincidir con `report.currency` (sin conversión de divisa en el tamaño).
    """

    name: str
    ticker: str
    isin: str | None = None
    currency: str = Field("EUR", pattern=r"^[A-Z]{3}$")
    asset_id: str | None = Field(None, description="Activo al que acompaña: la sección sale en los informes que lo incluyen")
    history_period: str = "max"
    filter_sma: int = Field(200, ge=20, description="Filtro: cierre por encima de la media de N sesiones")
    entry_breakout: int = Field(20, ge=5, description="Entrada: cierre por encima del máximo de las N sesiones previas")
    exit_low: int = Field(10, ge=2, description="Salida: cierre por debajo del mínimo de las N sesiones previas")
    atr_window: int = Field(20, ge=5)
    atr_multiple: float = Field(2.0, gt=0, description="Stop inicial = entrada − atr_multiple × ATR")
    capital_eur: float = Field(10000, gt=0)
    risk_pct: float = Field(1.0, gt=0, le=5, description="% del capital que se arriesga por operación (incluidos costes)")
    commission_pct: float = Field(0.1, ge=0, description="Comisión por operación (% del nominal)")
    commission_min_eur: float = Field(0.0, ge=0)
    spread_pct: float = Field(0.05, ge=0, description="Diferencial compra-venta completo (%); se paga la mitad en cada lado")
    exchange_tz: str = "Europe/Berlin"
    session_close: str = Field("17:35", pattern=r"^\d{2}:\d{2}$", description="Antes de esta hora la vela del día se descarta por incompleta")
    chart_sessions: int = Field(260, ge=60)
    enabled: bool = True

    @model_validator(mode="after")
    def _check_windows(self) -> TrendStrategyConfig:
        if self.exit_low >= self.entry_breakout:
            raise ValueError("exit_low debe ser menor que entry_breakout")
        return self


class RegimeStrategyConfig(_Strict):
    """Régimen semanal del S&P 500: SMA40 semanal con banda de tolerancia, exposición 0/100% entre un fondo indexado y un monetario.

    Señales sobre velas diarias de `ticker` (SPY), decididas solo al cierre semanal; el volumen y el VIX son solo contexto.
    Ver src/quant/regime.py.
    """

    name: str
    ticker: str = "SPY"
    vix_ticker: str | None = "^VIX"
    asset_id: str | None = Field(None, description="Activo al que acompaña (página de la web)")
    vehicle: str = Field("Fondo indexado S&P 500 ↔ fondo monetario EUR (traspasos)", description="Cómo se ejecuta")
    history_period: str = "max"
    sma_fast: int = Field(10, ge=2, description="Media rápida en semanas (solo contexto)")
    sma_slow: int = Field(40, ge=10, description="Media lenta en semanas: la que decide")
    exit_band_pct: float = Field(1.5, ge=0, le=10, description="Salir al 0% si el cierre semanal queda este % bajo la media lenta")
    enter_band_pct: float = Field(1.0, ge=0, le=10, description="Entrar al 100% si el cierre semanal queda este % sobre la media lenta")
    dd_window: int = Field(25, ge=5, description="Ventana de sesiones para contar días de distribución")
    dd_drop_pct: float = Field(0.2, gt=0, description="Caída mínima (%) de un día de distribución")
    dd_reduce: int = Field(7, ge=1, description="Días de distribución en la ventana que cuentan como aviso (contexto)")
    dd_aligned_max: int = Field(5, ge=0, description="Máximo de días de distribución para considerar el flujo alineado")
    cmf_window: int = Field(20, ge=5)
    obv_weeks: int = Field(10, ge=2, description="Divergencia OBV: precio sube y OBV baja frente a hace N semanas")
    vix_max: float = Field(25.0, gt=0, description="VIX semanal por encima = tensión macro")
    shock_day_pct: float = Field(4.0, gt=0, description="Caída de la última sesión de la semana que se considera choque aislado")
    execution_lag: int = Field(2, ge=0, description="Sesiones entre la decisión del viernes y la ejecución del traspaso (backtest)")
    cash_rate_pct: float = Field(0.0, ge=0, description="Rentabilidad anual supuesta del monetario en el backtest")
    exchange_tz: str = "America/New_York"
    session_close: str = Field("16:30", pattern=r"^\d{2}:\d{2}$", description="Antes de esta hora la vela del día se descarta por incompleta")
    chart_weeks: int = Field(156, ge=40)
    enabled: bool = True

    @model_validator(mode="after")
    def _check(self) -> RegimeStrategyConfig:
        if self.sma_fast >= self.sma_slow:
            raise ValueError("sma_fast debe ser menor que sma_slow")
        if self.dd_aligned_max >= self.dd_reduce:
            raise ValueError("dd_aligned_max debe ser menor que dd_reduce")
        return self


class ProfileConfig(_Strict):
    """Informe con nombre sobre un subconjunto de activos. Varios perfiles comparten datos, búsquedas y análisis."""

    title: str | None = Field(None, description="Subtítulo del informe; por defecto el nombre del perfil")
    description: str = ""
    assets: list[str] | Literal["all"] = Field("all", description="Ids de activo o 'all' (todos los habilitados)")


class AppConfig(_Strict):
    report: ReportConfig = ReportConfig()
    rules: RulesConfig = RulesConfig()
    data: DataConfig = DataConfig()
    research: ResearchConfig = ResearchConfig()
    llm: LLMConfig = LLMConfig()
    defaults: DefaultsConfig = DefaultsConfig()
    assets: list[AssetConfig] = Field(min_length=1)
    profiles: dict[str, ProfileConfig] = Field(default_factory=dict)
    strategies: dict[str, TrendStrategyConfig] = Field(default_factory=dict)
    regime_strategies: dict[str, RegimeStrategyConfig] = Field(default_factory=dict)

    @field_validator("assets")
    @classmethod
    def _unique_ids(cls, assets: list[AssetConfig]) -> list[AssetConfig]:
        ids = [a.id for a in assets]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"ids de activo duplicados: {duplicates}")
        return assets

    @model_validator(mode="after")
    def _check_profiles(self) -> AppConfig:
        known = {a.id for a in self.assets}
        for name, profile in self.profiles.items():
            if not re.fullmatch(r"[a-z0-9_]+", name):
                raise ValueError(f"nombre de perfil no válido: {name!r} (minúsculas, números y _)")
            if profile.assets == "all":
                continue
            if not profile.assets:
                raise ValueError(f"el perfil {name!r} no tiene activos")
            unknown = [i for i in profile.assets if i not in known]
            if unknown:
                raise ValueError(f"el perfil {name!r} usa activos desconocidos: {unknown}")
        for name, strategy in self.strategies.items():
            if not re.fullmatch(r"[a-z0-9_]+", name):
                raise ValueError(f"nombre de estrategia no válido: {name!r} (minúsculas, números y _)")
            if strategy.asset_id and strategy.asset_id not in known:
                raise ValueError(f"la estrategia {name!r} usa un activo desconocido: {strategy.asset_id!r}")
            if strategy.currency != self.report.currency:
                raise ValueError(f"la estrategia {name!r} cotiza en {strategy.currency}; debe coincidir con report.currency ({self.report.currency})")
        for name, regime in self.regime_strategies.items():
            if name in self.strategies or not re.fullmatch(r"[a-z0-9_]+", name):
                raise ValueError(f"nombre de estrategia de régimen no válido o repetido: {name!r}")
            if regime.asset_id and regime.asset_id not in known:
                raise ValueError(f"la estrategia {name!r} usa un activo desconocido: {regime.asset_id!r}")
        return self

    def strategies_for(self, asset_ids: list[str] | None) -> dict[str, TrendStrategyConfig]:
        """Estrategias habilitadas que acompañan a esos activos (o todas si `asset_ids` es None)."""
        return {n: s for n, s in self.strategies.items()
                if s.enabled and (asset_ids is None or s.asset_id is None or s.asset_id in asset_ids)}

    def benchmark_for(self, asset: AssetConfig) -> str:
        return asset.benchmark_ticker or self.defaults.benchmark_ticker

    def select_assets(self, ids: list[str] | None) -> list[AssetConfig]:
        """Activos habilitados, opcionalmente filtrados por id (en el orden de la config)."""
        if not ids:
            return [a for a in self.assets if a.enabled]
        known = {a.id for a in self.assets}
        unknown = [i for i in ids if i not in known]
        if unknown:
            raise ValueError(f"activos desconocidos: {unknown}. Disponibles: {sorted(known)}")
        return [a for a in self.assets if a.id in ids]

    def resolve_profiles(self, names: list[str]) -> dict[str, list[str]]:
        """Ids de activo de cada perfil pedido, en el orden de la config."""
        unknown = [n for n in names if n not in self.profiles]
        if unknown:
            raise ValueError(f"perfiles desconocidos: {unknown}. Disponibles: {sorted(self.profiles)}")
        resolved = {}
        for name in names:
            ids = self.profiles[name].assets
            resolved[name] = [a.id for a in self.select_assets(None if ids == "all" else ids)]
        return resolved
