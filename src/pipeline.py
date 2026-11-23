"""Orquestador: encadena las etapas y construye el `RunRecord`.

Etapas (cada una degradable de forma independiente):
  1. market     datos de mercado + cálculos (Python)
  2. research   búsqueda web (Python + Tavily)
  3. macro      agente macro (LLM)
  4. assets     agentes por activo (LLM)
  5. supervisor agente supervisor (LLM) o síntesis determinista
  6. synthesis  guardrails, importes, contradicciones y datos faltantes (Python)

Con perfiles (`RunOptions.profiles`) las etapas 1-4 se hacen una vez sobre la unión de activos
y las 5-6 una vez por perfil, con un `RunRecord` (e informe) por perfil.
"""

from __future__ import annotations

import asyncio
import logging
import platform
from dataclasses import dataclass
from pathlib import Path
from datetime import date, datetime
from importlib import metadata
from typing import Any, Literal

from src.analysis import prompts
from src.analysis.agents import build_agents
from src.analysis.anthropic_runner import AnthropicRunner, build_instructions
from src.analysis.handoff_runner import HandoffRunner
from src.analysis.runner import AgentRunner
from src.analysis.schemas import AssetAnalysis, MacroAnalysis, SupervisorOutput
from src.config.models import AppConfig, AssetConfig
from src.core.cache import FileCache
from src.core.errors import ConfigError, LLMError, LLMPendingError, LLMUnavailableError
from src.market.providers.yfinance_provider import YFinanceProvider
from src.market.service import MarketDataService
from src.models import AssetSnapshot, GuardrailEvent, RunRecord, Source
from src.quant.indicators import build_indicators, load_cape
from src.quant.snapshot import build_snapshot
from src.quant.trend_snapshot import build_trend_strategy
from src.research.providers.tavily_provider import TavilyProvider
from src.research.service import ResearchService
from src.settings import Settings
from src.synthesis import contradictions, guardrails, missing_data
from src.synthesis.decisions import build_decisions, fallback_supervisor

logger = logging.getLogger(__name__)
Mode = Literal["full", "no-web", "quant"]


@dataclass
class RunOptions:
    asset_ids: list[str] | None = None
    mode: Mode = "full"
    skip_macro: bool = False
    skip_supervisor: bool = False
    use_cache: bool = True
    handoff_dir: Path | None = None
    profiles: dict[str, list[str]] | None = None  # nombre -> ids; los activos analizados son la unión


def _versions() -> dict[str, str]:
    versions = {"python": platform.python_version()}
    for pkg in ("openai-agents", "yfinance", "tavily-python", "pandas"):
        try:
            versions[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            pass
    return versions


def build_market_service(config: AppConfig, settings: Settings, use_cache: bool = True) -> MarketDataService:
    cache_root = settings.cache_dir
    return MarketDataService(
        YFinanceProvider(),
        YFinanceProvider(),
        FileCache(cache_root / "prices", config.data.price_cache_ttl_hours, enabled=use_cache),
        FileCache(cache_root / "fundamentals", config.data.fundamentals_cache_ttl_hours, enabled=use_cache),
        config.data.history_period,
    )


class Pipeline:
    def __init__(self, config: AppConfig, settings: Settings, options: RunOptions, run_id: str) -> None:
        self.config = config
        self.settings = settings
        self.options = options
        self.today = date.today()
        self.assets: list[AssetConfig] = config.select_assets(options.asset_ids)
        self.record = RunRecord(
            run_id=run_id,
            created_at=datetime.now().isoformat(timespec="seconds"),
            mode=options.mode,
            report_currency=config.report.currency,
            assets_requested=[a.id for a in self.assets],
            config_snapshot=config.model_dump(),
            versions=_versions(),
        )
        self.llm_errors: dict[str, str] = {}
        self.llm_disabled: str | None = None
        self.pending: list[str] = []
        self._llm: tuple[Any, dict[str, Any]] = (None, {})
        self.resumed = self._load_handoff_state()
        cache_root = settings.cache_dir
        use = options.use_cache
        self.market = build_market_service(config, settings, use)
        self.sentiment_cache = FileCache(cache_root / "sentiment", config.data.price_cache_ttl_hours, enabled=use)
        self.macro_cache = FileCache(cache_root / "macro", config.data.macro_cache_ttl_hours, enabled=use) if config.data.macro_data else None
        self.research = self._build_research(FileCache(cache_root / "search", config.research.cache_ttl_hours, enabled=use))

    # ------------------------------------------------------------------ setup
    @property
    def _state_path(self) -> Path | None:
        return self.options.handoff_dir / "state.json" if self.options.handoff_dir else None

    def _load_handoff_state(self) -> bool:
        """En modo handoff reutiliza datos y fuentes de la primera ejecución para que no cambien entre pasos."""
        path = self._state_path
        if path is None or not path.exists():
            return False
        saved = RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
        if saved.assets_requested != self.record.assets_requested or saved.mode != self.record.mode:
            raise ConfigError(f"{path} es de otra ejecución (activos {saved.assets_requested}, modo {saved.mode}). Usa otra carpeta --handoff.")
        self.record = saved
        logger.info("Handoff: reanudando la ejecución %s desde %s", saved.run_id, path)
        return True

    def _build_research(self, cache: FileCache) -> ResearchService:
        cfg = self.config.research
        if self.options.mode != "full":
            return ResearchService(None, cfg, cache, disabled_reason=f"modo {self.options.mode}")
        if not cfg.enabled:
            return ResearchService(None, cfg, cache, disabled_reason="research.enabled=false en la configuración")
        if not self.settings.tavily_api_key:
            self.record.errors.append("TAVILY_API_KEY no definida: se continúa sin búsqueda web")
            return ResearchService(None, cfg, cache, disabled_reason="TAVILY_API_KEY no definida")
        return ResearchService(TavilyProvider(self.settings.tavily_api_key), cfg, cache)

    # ----------------------------------------------------------------- stages
    def stage_market(self) -> dict[str, AssetSnapshot]:
        logger.info("Etapa market: %d activos", len(self.assets))
        cape_data = load_cape(self.macro_cache) if self.macro_cache else None
        snapshots = {a.id: build_snapshot(a, self.config, self.market, cape_data) for a in self.assets}
        self.record.indicators = build_indicators(self.market, self.sentiment_cache, self.macro_cache, cape_data)
        self.record.snapshots = list(snapshots.values())
        self.record.strategies = [build_trend_strategy(name, cfg, self.market)
                                  for name, cfg in self.config.strategies_for(self.record.assets_requested).items()]
        for strat in self.record.strategies:
            if strat.error:
                self.record.errors.append(f"estrategia {strat.id}: {strat.error}")
        for snap in snapshots.values():
            if snap.error:
                self.record.errors.append(f"{snap.asset_id}: {snap.error}")
        return snapshots

    def stage_research(self, snapshots: dict[str, AssetSnapshot]) -> None:
        if not self.research.available and self.research.usage.disabled_reason:
            logger.info("Etapa research omitida: %s", self.research.usage.disabled_reason)
        else:
            cfg = self.config.research
            logger.info("Etapa research (presupuesto %d búsquedas)", cfg.max_searches_per_run)
            plan: list[tuple[str, str, str, list[str]]] = []  # (consulta, ámbito, periodo, webs preferidas)
            if not self.options.skip_macro:
                plan += [(q.query, "macro", q.time_range, cfg.preferred_domains.get("macro", [])) for q in cfg.macro_queries]
                plan += [(q.query, "events", q.time_range, cfg.preferred_domains.get("events", [])) for q in cfg.events_queries]
            for asset in self.assets:
                if snapshots[asset.id].prices is None:
                    continue
                query = cfg.asset_query_template.format(search_terms=asset.search_label, name=asset.name, ticker=asset.ticker)
                domains = asset.preferred_domains if asset.preferred_domains is not None else cfg.preferred_domains.get(asset.asset_class, [])
                plan.append((query, asset.id, "week", domains))
            # Primero todas las búsquedas en webs preferidas; después, si queda presupuesto, se amplían las que dieron poco.
            weak = [(q, scope, tr) for q, scope, tr, domains in plan
                    if len(self.research.search(q, scope=scope, time_range=tr, domains=domains)) < cfg.min_preferred_results and domains]
            for q, scope, tr in weak:
                logger.info("Pocas fuentes preferidas para '%s': búsqueda abierta", scope)
                self.research.search(q, scope=scope, time_range=tr)
        self.record.sources = self.research.sources
        self.record.search_usage = self.research.usage

    def _llm_ready(self) -> bool:
        if self.options.mode == "quant":
            return False
        if self.options.handoff_dir:
            return True
        if self.llm_disabled:
            return False
        # Con Anthropic no se exige la variable: el SDK también admite el perfil de `ant auth login`.
        if self.config.llm.provider == "openai" and not self.settings.openai_api_key:
            self.llm_disabled = "OPENAI_API_KEY no definida"
            self.record.errors.append("OPENAI_API_KEY no definida: informe solo cuantitativo, sin extras")
            return False
        return True

    def _build_llm(self) -> tuple[Any, dict[str, Any]]:
        """Runner y 'agentes' del proveedor configurado. Ambos runners exponen run(agente, prompt, tipo, etiqueta)."""
        llm = self.config.llm
        model = self.settings.openai_model or llm.model
        if self.options.handoff_dir:
            return HandoffRunner(self.options.handoff_dir), build_instructions()
        if llm.provider == "anthropic":
            runner = AnthropicRunner(model=model, effort=llm.effort, max_retries=llm.max_retries,
                                     timeout_seconds=llm.timeout_seconds, api_key=self.settings.anthropic_api_key)
            return runner, build_instructions()
        return AgentRunner(max_retries=llm.max_retries, timeout_seconds=llm.timeout_seconds, tracing=llm.tracing), build_agents(model)

    async def _call(self, runner: Any, agent: Any, prompt: str, output_type: type, label: str) -> Any | None:
        if not self._llm_ready():
            return None
        try:
            return await runner.run(agent, prompt, output_type, label)
        except LLMPendingError as exc:
            logger.warning("%s", exc)
            self.pending.append(label)
        except LLMUnavailableError as exc:
            logger.error("%s. Se desactiva el LLM para el resto de la ejecución.", exc)
            self.llm_disabled = str(exc)
            self.llm_errors[label] = str(exc)
            self.record.errors.append(str(exc))
        except LLMError as exc:
            logger.error("%s", exc)
            self.llm_errors[label] = str(exc)
            self.record.errors.append(str(exc))
        except Exception as exc:  # un fallo inesperado de un agente no debe impedir el informe
            logger.exception("Error inesperado en el agente %s", label)
            message = f"Error inesperado en el agente {label}: {type(exc).__name__}: {exc}"
            self.llm_errors[label] = message
            self.record.errors.append(message)
        return None

    def _sources(self, scopes: set[str]) -> list[Source]:
        return [s for s in self.record.sources if s.scope in scopes]

    async def stage_analysis(self, snapshots: dict[str, AssetSnapshot]) -> set[str]:
        """Ejecuta los agentes macro y de activo (comunes a todos los perfiles). Devuelve los activos bloqueados por guardrails."""
        blocked: set[str] = set()
        if not self._llm_ready():
            return blocked
        runner, agents = self._llm
        known_ids = {s.id for s in self.record.sources}
        today = self.today.isoformat()
        events: list[GuardrailEvent] = []

        macro: MacroAnalysis | None = None
        if not self.options.skip_macro:
            logger.info("Etapa macro")
            macro = await self._call(runner, agents["macro"], prompts.macro_prompt(today, self._sources({"macro", "events"}), self.record.indicators), MacroAnalysis, "macro")
            if macro:
                macro, ev = guardrails.sanitize_macro(macro, known_ids)
                events += ev
        self.record.macro = macro
        if self.pending:  # los activos necesitan el resumen macro
            return blocked
        brief = (
            f"Régimen {macro.regime}; riesgo renta variable {macro.equity_risk_level} ({macro.equity_risk_score}/10); "
            f"inflación {macro.inflation_trend}; tipos {macro.rates_trend}; liquidez {macro.liquidity}; confianza {macro.confidence}.\n{macro.summary}"
            if macro else "No disponible: trata el contexto macro como desconocido y reduce la convicción."
        )

        logger.info("Etapa assets")
        semaphore = asyncio.Semaphore(self.config.llm.max_concurrency)

        async def analyse(asset: AssetConfig) -> tuple[str, AssetAnalysis | None]:
            snap = snapshots[asset.id]
            if snap.prices is None:
                return asset.id, None
            async with semaphore:
                prompt = prompts.asset_prompt(asset, snap, brief, self._sources({asset.id}), self.config, today, self.record.indicators)
                return asset.id, await self._call(runner, agents["asset"], prompt, AssetAnalysis, asset.id)

        analyses: dict[str, AssetAnalysis] = {}
        for asset_id, result in await asyncio.gather(*(analyse(a) for a in self.assets)):
            if result is None:
                continue
            cleaned, ev, block = guardrails.sanitize_asset(result, asset_id, known_ids)
            analyses[asset_id] = cleaned
            events += ev
            if block:
                blocked.add(asset_id)
        self.record.asset_analyses = analyses
        self.record.guardrail_events += events
        return blocked

    async def stage_supervisor(self, rec: RunRecord, assets: list[AssetConfig], snapshots: dict[str, AssetSnapshot], label: str) -> set[str]:
        """Supervisor LLM (o síntesis determinista) sobre los activos de `rec`. Devuelve los activos que bloquea."""
        blocked: set[str] = set()
        analyses = rec.asset_analyses
        supervisor: SupervisorOutput | None = None
        if analyses and not self.options.skip_supervisor and self._llm_ready():
            logger.info("Etapa supervisor (%s)", label)
            runner, agents = self._llm
            payload = {
                "reglas": {"horizonte_anos": self.config.rules.horizon_years, "max_activos_con_entrada": self.config.rules.max_assets_with_extra},
                "indicadores": [i.model_dump(exclude={"note", "source"}) for i in rec.indicators],
                "macro": rec.macro.model_dump() if rec.macro else None,
                "activos": [
                    {"asset_id": a.id, "nombre": a.name, "clase": a.asset_class,
                     "datos": prompts.snapshot_payload(snapshots[a.id]) if snapshots[a.id].prices else "sin datos",
                     "analisis": analyses[a.id].model_dump() if a.id in analyses else None}
                    for a in assets
                ],
            }
            short = [s.model_copy(update={"snippet": s.snippet[:250]}) for s in rec.sources]
            supervisor = await self._call(runner, agents["supervisor"], prompts.supervisor_prompt(payload, short, self.today.isoformat()), SupervisorOutput, label)
            if label in self.pending:
                return blocked
            if supervisor:
                supervisor, ev, blocked = guardrails.sanitize_supervisor(supervisor, {a.id for a in assets})
                rec.guardrail_events += ev
        if analyses and supervisor is None:
            reason = "omitido por opción" if self.options.skip_supervisor else self.llm_errors.get(label, "no disponible")
            supervisor = fallback_supervisor(analyses, rec.macro, assets, self.config.rules.min_score_for_extra, reason)
            rec.supervisor_is_fallback = True
        rec.supervisor = supervisor
        return blocked

    def stage_synthesis(self, rec: RunRecord, assets: list[AssetConfig], snapshots: dict[str, AssetSnapshot], blocked: set[str]) -> None:
        logger.info("Etapa synthesis%s", f" ({rec.profile})" if rec.profile else "")
        decisions, events = build_decisions(self.config, assets, snapshots, rec.asset_analyses, rec.supervisor, rec.macro, blocked, self.today)
        rec.decisions = decisions
        rec.guardrail_events += events
        rec.contradictions = contradictions.detect(snapshots, rec.asset_analyses, decisions, rec.macro, None if rec.supervisor_is_fallback else rec.supervisor)
        effective_mode = "quant" if (self.options.mode == "quant" or (self.llm_disabled and not rec.asset_analyses)) else self.options.mode
        rec.missing_data = missing_data.collect(rec.snapshots, rec.asset_analyses, rec.macro, rec.sources, rec.search_usage, effective_mode, self.llm_errors)
        if self.llm_disabled and not rec.asset_analyses and self.options.mode != "quant":
            rec.missing_data.insert(0, missing_data.MissingDataItem(scope="global", item=f"LLM no disponible: {self.llm_disabled}", impact="Sin análisis cualitativo; no se recomiendan extras"))

    def _profile_record(self, name: str, ids: list[str]) -> RunRecord:
        """Copia del registro común limitada a los activos del perfil."""
        keep = set(ids)
        base = self.record
        return base.model_copy(deep=True, update={
            "run_id": f"{base.run_id}-{name}",
            "profile": name,
            "assets_requested": ids,
            "snapshots": [s for s in base.snapshots if s.asset_id in keep],
            "sources": [s for s in base.sources if s.scope in keep | {"macro", "events"}],
            "asset_analyses": {k: v for k, v in base.asset_analyses.items() if k in keep},
            "guardrail_events": [e for e in base.guardrail_events if e.asset_id is None or e.asset_id in keep],
            "strategies": [s for s in base.strategies if s.asset_id is None or s.asset_id in keep],
        })

    async def run_all(self) -> list[RunRecord]:
        """Ejecuta todas las etapas. Devuelve un registro por perfil (o uno solo sin perfiles).

        En modo handoff, si quedan respuestas pendientes (`self.pending`) los registros están incompletos.
        """
        if self.resumed:
            snapshots = {s.asset_id: s for s in self.record.snapshots}
        else:
            snapshots = self.stage_market()
        has_data = any(s.prices for s in snapshots.values())
        blocked: set[str] = set()
        if has_data:
            if not self.resumed:
                self.stage_research(snapshots)
                if self._state_path:
                    self._state_path.parent.mkdir(parents=True, exist_ok=True)
                    self._state_path.write_text(self.record.model_dump_json(indent=1), encoding="utf-8")
            if self._llm_ready():
                self._llm = self._build_llm()
            blocked = await self.stage_analysis(snapshots)
            if self.pending:
                return [self.record]
        else:
            logger.error("Ningún activo tiene datos de mercado: se omiten búsqueda y LLM")
            self.record.errors.append("Ningún activo tiene datos de mercado (¿sin conexión?)")

        if self.options.profiles:
            targets = [(self._profile_record(name, ids), name, ids) for name, ids in self.options.profiles.items()]
        else:
            targets = [(self.record, None, self.record.assets_requested)]
        by_id = {a.id: a for a in self.assets}
        records: list[RunRecord] = []
        for rec, name, ids in targets:
            assets = [by_id[i] for i in ids]
            subset = {i: snapshots[i] for i in ids}
            label = f"supervisor_{name}" if name else "supervisor"
            sup_blocked = await self.stage_supervisor(rec, assets, subset, label) if has_data else set()
            if label in self.pending:
                continue
            self.stage_synthesis(rec, assets, subset, (blocked & set(ids)) | sup_blocked)
            records.append(rec)
        return records

    async def run(self) -> RunRecord:
        """Ejecución sin perfiles (o el primero de ellos)."""
        records = await self.run_all()
        return records[0] if records else self.record
