"""Modelos de dominio compartidos entre capas (datos, búsqueda, síntesis, informe, auditoría).

Convención: una métrica no disponible se guarda como `None` y se muestra/serializa
para el LLM como "insufficient_data". Nunca se rellena con estimaciones.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from src.analysis.schemas import AssetAnalysis, MacroAnalysis, SupervisorOutput
from src.config.models import Action

INSUFFICIENT = "insufficient_data"
PERIODS: tuple[str, ...] = ("1m", "3m", "6m", "12m")

QuantTrend = Literal["up", "sideways", "down", "insufficient_data"]
QualityLevel = Literal["good", "partial", "poor", "unavailable"]
Severity = Literal["info", "warning", "blocking"]


class Source(BaseModel):
    """Fuente web citada. El `id` (S1, S2...) es lo que el LLM usa para citar."""

    id: str
    title: str
    url: str
    published_date: str | None = None
    retrieved_at: str
    query: str
    scope: str = Field(description="'macro', 'events' o asset_id")
    snippet: str = ""
    provider: str = "tavily"


class PriceMetrics(BaseModel):
    """Métricas calculadas en Python a partir de la serie de precios."""

    currency: str
    last_price: float
    last_date: str
    observations: int
    returns_pct: dict[str, float | None]
    returns_report_ccy_pct: dict[str, float | None]
    report_currency: str
    sma50: float | None
    sma200: float | None
    above_sma50: bool | None
    above_sma200: bool | None
    recent_high: float | None
    recent_high_date: str | None
    drawdown_from_recent_high_pct: float | None
    max_drawdown_window_pct: float | None
    volatility_annualized_pct: float | None
    benchmark_ticker: str | None
    benchmark_returns_report_ccy_pct: dict[str, float | None]
    relative_vs_benchmark_pp: dict[str, float | None]
    quant_trend: QuantTrend
    sparkline: list[float] = Field(default_factory=list, description="Cierres semanales de los últimos 12 meses (divisa local)")


class ValuationMetrics(BaseModel):
    """Métricas de valuación. Solo se rellenan con datos del proveedor; si no, None."""

    source_ticker: str | None = None
    provider: str | None = None
    as_of: str | None = None
    trailing_pe: float | None = None
    forward_pe: float | None = None
    cape: float | None = None
    cape_source: str | None = None
    ev_ebitda: float | None = None
    price_to_sales: float | None = None
    dividend_yield_pct: float | None = None
    percentiles: dict[str, float | None] = Field(default_factory=dict)


class MarketIndicator(BaseModel):
    """Indicador global de mercado (VIX, tipos, dólar, miedo y codicia...). Calculado u obtenido por Python."""

    id: str
    name: str
    value: float | None
    unit: str = ""
    kind: Literal["sentiment", "volatility", "rates", "fx", "inflation", "credit", "activity", "valuation"]
    as_of: str | None = None
    rating: str | None = None
    previous: dict[str, float | None] = Field(default_factory=dict)
    source: str = ""
    note: str = ""


class CapeStats(BaseModel):
    """CAPE (Shiller PE) actual y su posición en la historia. Percentiles 0-100 calculados por Python."""

    value: float
    as_of: str
    history_start: str
    previous: dict[str, float | None] = Field(default_factory=dict)
    percentile_all: float
    percentile_30y: float
    median_all: float
    median_30y: float
    source: str = "multpl.com (datos de R. Shiller)"


class DataQuality(BaseModel):
    level: QualityLevel
    issues: list[str] = Field(default_factory=list)


class AssetSnapshot(BaseModel):
    """Todo lo objetivo que sabemos de un activo en esta ejecución."""

    asset_id: str
    name: str
    ticker: str
    asset_class: str
    prices: PriceMetrics | None = None
    valuation: ValuationMetrics | None = None
    quality: DataQuality
    error: str | None = None
    used_stale_cache: bool = False


class GuardrailEvent(BaseModel):
    rule: str
    message: str
    severity: Severity = "warning"
    asset_id: str | None = None
    before: str | None = None
    after: str | None = None


class Contradiction(BaseModel):
    description: str
    asset_ids: list[str] = Field(default_factory=list)
    origin: str = "python"
    resolution: str = ""


class MissingDataItem(BaseModel):
    scope: str
    item: str
    impact: str = ""


class Tranche(BaseModel):
    amount_eur: float
    planned_date: str


class AssetDecision(BaseModel):
    """Decisión final por activo tras aplicar los guardrails deterministas."""

    asset_id: str
    name: str
    opportunity_score: int | None
    conviction: str | None
    analyst_action: Action | None
    supervisor_action: Action | None
    proposed_action: Action
    final_action: Action
    amount_eur: float = 0.0
    tranches: list[Tranche] = Field(default_factory=list)
    rationale: str = ""
    conditions: list[str] = Field(default_factory=list)
    invalidation: list[str] = Field(default_factory=list)


TrendState = Literal["sin_posicion", "entrada_pendiente", "mantener", "salida_pendiente", "insufficient_data"]


class TrendTrade(BaseModel):
    """Operación teórica reconstruida aplicando las reglas al histórico (señal al cierre, ejecución en la apertura siguiente)."""

    signal_date: str
    entry_date: str
    entry_price: float
    initial_stop: float
    atr_at_signal: float
    shares: int
    exit_signal_date: str | None = None
    exit_date: str | None = None
    exit_price: float | None = None
    exit_reason: Literal["stop", "minimo"] | None = None
    r_multiple: float | None = Field(None, description="(salida − entrada) / (entrada − stop inicial), sin costes")
    return_pct: float | None = None

    @property
    def is_open(self) -> bool:
        return self.exit_date is None


class TrendRisk(BaseModel):
    """Riesgo y tamaño de la operación (prevista, en curso o hipotética). Importes en la divisa de la estrategia."""

    basis: Literal["prevista", "en_curso", "hipotetica"]
    entry_price: float
    initial_stop: float
    active_stop: float
    risk_per_share: float
    risk_budget: float
    max_shares: int
    position_value: float
    costs: float = Field(description="Comisiones y medio diferencial en entrada y en salida al stop")
    total_risk: float = Field(description="max_shares × riesgo por participación + costes")
    capped_by_capital: bool = False


class TrendAudit(BaseModel):
    rule: str
    provider: str
    ticker: str
    period: str
    first_date: str
    last_date: str
    observations: int
    data_sha256: str
    stale_cache: bool = False
    dropped_incomplete_bar: str | None = None
    generated_at: str
    params: dict[str, Any] = Field(default_factory=dict)


class TrendStats(BaseModel):
    trades: int = 0
    closed: int = 0
    win_rate_pct: float | None = None
    avg_r: float | None = None
    total_r: float | None = None
    best_r: float | None = None
    worst_r: float | None = None
    exposure_pct: float | None = Field(None, description="% de sesiones con posición abierta")
    since: str | None = None


class TrendChart(BaseModel):
    """Series de las últimas sesiones para el gráfico (None donde el indicador aún no existe)."""

    dates: list[str] = Field(default_factory=list)
    close: list[float] = Field(default_factory=list)
    sma: list[float | None] = Field(default_factory=list)
    upper: list[float | None] = Field(default_factory=list)
    lower: list[float | None] = Field(default_factory=list)


class TrendStrategyResult(BaseModel):
    """Estado de una estrategia de seguimiento de tendencia. Todo calculado por Python."""

    id: str
    name: str
    ticker: str
    isin: str | None = None
    currency: str
    asset_id: str | None = None
    state: TrendState = "insufficient_data"
    last_date: str | None = None
    last_close: float | None = None
    sma: float | None = None
    filter_pass: bool | None = None
    entry_level: float | None = Field(None, description="Máximo de las N sesiones previas")
    entry_signal: bool | None = None
    distance_to_entry_pct: float | None = None
    exit_level: float | None = Field(None, description="Mínimo de las N sesiones previas")
    atr: float | None = None
    atr_pct: float | None = None
    position: TrendTrade | None = None
    risk: TrendRisk | None = None
    explanation: list[str] = Field(default_factory=list)
    trades: list[TrendTrade] = Field(default_factory=list)
    stats: TrendStats = TrendStats()
    chart: TrendChart = TrendChart()
    audit: TrendAudit | None = None
    error: str | None = None


RegimeTrend = Literal["alcista", "bajista", "transicion"]


class RegimeCheck(BaseModel):
    """Un dato del checklist del viernes: valor, umbral de la regla y lectura."""

    id: str
    label: str
    value: str
    rule: str
    status: Literal["ok", "aviso", "alerta", "na"]
    note: str = ""


class RegimeSwitch(BaseModel):
    date: str = Field(description="Viernes (última sesión de la semana) de la decisión")
    from_pct: int
    to_pct: int
    reasons: list[str] = Field(default_factory=list)


class RegimePerf(BaseModel):
    cagr_pct: float | None = None
    vol_pct: float | None = None
    max_dd_pct: float | None = None
    worst_year_pct: float | None = None
    total_pct: float | None = None
    avg_exposure_pct: float | None = None


class RegimeChart(BaseModel):
    """Velas semanales recientes para el gráfico (None donde la media aún no existe)."""

    dates: list[str] = Field(default_factory=list)
    close: list[float] = Field(default_factory=list)
    sma_fast: list[float | None] = Field(default_factory=list)
    sma_slow: list[float | None] = Field(default_factory=list)
    exit_line: list[float | None] = Field(default_factory=list)
    exposure: list[int | None] = Field(default_factory=list)
    dd25: list[int | None] = Field(default_factory=list)


class FlowDay(BaseModel):
    date: str
    change_pct: float
    volume_change_pct: float


class RegimeStrategyResult(BaseModel):
    """Estado de la estrategia de régimen semanal (SMA40 con banda de tolerancia). Todo calculado por Python."""

    id: str
    name: str
    ticker: str
    vehicle: str = ""
    asset_id: str | None = None
    exposure: int | None = Field(None, description="Exposición vigente (%) según la última decisión semanal")
    decision_date: str | None = Field(None, description="Última semana cerrada evaluada")
    changed: bool = Field(False, description="La última decisión cambia la exposición: hay que ordenar un traspaso")
    previous_exposure: int | None = None
    provisional_exposure: int | None = Field(None, description="Lo que diría la regla si la semana en curso cerrara hoy")
    provisional_reasons: list[str] = Field(default_factory=list)
    week_complete: bool = True
    shock_week: bool = False
    last_date: str | None = None
    last_close: float | None = None
    sma_fast: float | None = None
    sma_slow: float | None = None
    exit_line: float | None = Field(None, description="Cierre semanal por debajo del cual se sale (SMA lenta − banda)")
    enter_line: float | None = Field(None, description="Cierre semanal por encima del cual se entra (SMA lenta + banda)")
    gap_pct: float | None = Field(None, description="Distancia del cierre a la SMA lenta (%)")
    trend: RegimeTrend | None = None
    dd25: int | None = None
    distribution_days: list[FlowDay] = Field(default_factory=list)
    cmf: float | None = None
    obv_divergence: bool | None = None
    vix: float | None = None
    checks: list[RegimeCheck] = Field(default_factory=list)
    explanation: list[str] = Field(default_factory=list)
    switches: list[RegimeSwitch] = Field(default_factory=list)
    switches_per_year: float | None = None
    since: str | None = None
    perf: dict[str, RegimePerf] = Field(default_factory=dict)
    episodes: list[dict[str, Any]] = Field(default_factory=list)
    chart: RegimeChart = RegimeChart()
    audit: TrendAudit | None = None
    error: str | None = None


class FactorRegime(BaseModel):
    """Rentabilidad posterior del activo en los meses en que el factor subía (o bajaba)."""

    months: int = 0
    avg_return_1m_pct: float | None = None
    avg_return_3m_pct: float | None = None
    up_share_3m_pct: float | None = Field(None, description="% de meses con rentabilidad a 3 meses positiva")
    std_3m_pct: float | None = None


class MacroFactor(BaseModel):
    """Un factor macro y su relación histórica con el activo (solo informativo: no cambia ninguna señal)."""

    id: str
    name: str
    source: str
    unit: str = ""
    change_unit: str = Field(description="'pb' (puntos básicos), 'pts' o '%': cómo se mide la variación del factor")
    usual: str = Field(description="Relación que suele citarse con el oro")
    usual_sign: Literal[-1, 1]
    value: float | None = None
    as_of: str | None = None
    change_3m: float | None = None
    direction: Literal["up", "down", "flat", "insufficient_data"] = "insufficient_data"
    rising: FactorRegime = FactorRegime()
    falling: FactorRegime = FactorRegime()
    edge_3m_pp: float | None = Field(None, description="Rentabilidad media a 3m en el régimen actual menos en el contrario")
    t_stat: float | None = Field(None, description="Diferencia de medias entre regímenes / error estándar, con muestra efectiva = meses / 3")
    observed_sign: Literal[-1, 0, 1] | None = None
    corr_52w: float | None = None
    signal: Literal["favorable", "unfavorable", "neutral", "insufficient_data"] = "insufficient_data"
    sparkline: list[float] = Field(default_factory=list)
    note: str = ""
    error: str | None = None


class FactorStudy(BaseModel):
    """Estudio de factores macro de un activo: estado actual y estadística histórica."""

    asset_name: str
    price_basis: str
    since: str | None = None
    until: str | None = None
    months: int = 0
    base_avg_3m_pct: float | None = None
    base_up_share_3m_pct: float | None = None
    factors: list[MacroFactor] = Field(default_factory=list)
    favorable: int = 0
    unfavorable: int = 0
    neutral: int = 0
    method: list[str] = Field(default_factory=list)
    generated_at: str = ""
    error: str | None = None


class SearchUsage(BaseModel):
    budget: int = 0
    performed: int = 0
    cache_hits: int = 0
    failed: int = 0
    quota_exhausted: bool = False
    disabled_reason: str | None = None


class RunRecord(BaseModel):
    """Registro completo de una ejecución. Se guarda en JSON y permite regenerar el informe."""

    run_id: str
    created_at: str
    mode: str
    profile: str | None = None
    report_currency: str
    assets_requested: list[str]
    config_snapshot: dict[str, Any]
    snapshots: list[AssetSnapshot] = Field(default_factory=list)
    indicators: list[MarketIndicator] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    macro: MacroAnalysis | None = None
    asset_analyses: dict[str, AssetAnalysis] = Field(default_factory=dict)
    supervisor: SupervisorOutput | None = None
    supervisor_is_fallback: bool = False
    decisions: list[AssetDecision] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    missing_data: list[MissingDataItem] = Field(default_factory=list)
    guardrail_events: list[GuardrailEvent] = Field(default_factory=list)
    strategies: list[TrendStrategyResult] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    search_usage: SearchUsage = SearchUsage()
    report_path: str | None = None
    log_path: str | None = None
    versions: dict[str, str] = Field(default_factory=dict)

    def snapshot_for(self, asset_id: str) -> AssetSnapshot | None:
        return next((s for s in self.snapshots if s.asset_id == asset_id), None)

    @property
    def total_extra_eur(self) -> float:
        return round(sum(d.amount_eur for d in self.decisions), 2)
