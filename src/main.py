"""Punto de entrada CLI: `python -m src.main [opciones]`.

Ejemplos:
  python -m src.main                              # análisis completo
  python -m src.main --asset msci_world           # un activo
  python -m src.main --assets msci_world,bitcoin  # varios activos
  python -m src.main --profiles semanal,refugio   # un informe por perfil, con datos y análisis compartidos
  python -m src.main --mode quant                 # solo datos y cálculos, sin APIs de pago
  python -m src.main --render data/runs/run_X.json  # regenerar HTML de una ejecución guardada
  python -m src.main --site                       # web pública en docs/ (último análisis + oro recalculado)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path

from src.analysis.handoff_runner import RESULT_FILE, handoff_status
from src.config import AppConfig, load_config
from src.core.errors import ConfigError, ReportError
from src.core.logging_setup import setup_logging
from src.models import RunRecord
from src.core.cache import FileCache
from src.pipeline import Pipeline, RunOptions, build_market_service
from src.quant.gold_factors import build_factor_study
from src.quant.regime_snapshot import build_regime_strategy
from src.quant.trend_snapshot import build_trend_strategy
from src.reporting.builder import ACTION_LABELS, TREND_STATE_LABELS
from src.reporting.html import write_report, write_strategy_report
from src.reporting.site import GOLD_ASSET_ID, SP500_ASSET_ID, build_site
from src.settings import PROJECT_ROOT, Settings, load_settings
from src.storage.run_store import RunStore

logger = logging.getLogger("src.main")

EXIT_OK, EXIT_CONFIG, EXIT_REPORT, EXIT_DEGRADED, EXIT_PENDING = 0, 2, 3, 4, 5

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m src.main", description="Informe periódico sobre el momento de invertir en cada activo.")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config.yaml", help="Ruta al YAML de configuración")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--asset", help="Analizar un solo activo (id de la config)")
    group.add_argument("--assets", help="Lista de ids separados por comas")
    group.add_argument("--profiles", "--profile", dest="profiles", help="Perfiles de config separados por comas: un informe por perfil")
    parser.add_argument("--mode", choices=["full", "no-web", "quant"], default="full",
                        help="full: todo · no-web: sin Tavily · quant: solo datos y cálculos (sin LLM ni web)")
    parser.add_argument("--skip-macro", action="store_true", help="No ejecutar búsquedas ni agente macro")
    parser.add_argument("--skip-supervisor", action="store_true", help="Usar síntesis determinista en lugar del supervisor LLM")
    parser.add_argument("--handoff", type=Path, metavar="DIR",
                        help="LLM externo por ficheros: escribe peticiones en DIR/requests y lee respuestas de DIR/responses")
    parser.add_argument("--handoff-status", type=Path, metavar="DIR", help="Estado de una carpeta handoff en JSON y salir")
    parser.add_argument("--no-cache", action="store_true", help="Ignorar la caché local (fuerza descargas y búsquedas)")
    parser.add_argument("--output-dir", type=Path, help="Carpeta de salida del HTML (por defecto report.output_dir)")
    parser.add_argument("--render", type=Path, metavar="RUN_JSON", help="Regenerar el HTML a partir de un JSON guardado (sin APIs)")
    parser.add_argument("--list-assets", action="store_true", help="Listar activos configurados y salir")
    parser.add_argument("--strategy", "--strategies", dest="strategy", metavar="NOMBRES",
                        help="Solo las estrategias de tendencia indicadas (o 'all'): informe propio sin LLM ni web")
    parser.add_argument("--site", type=Path, nargs="?", const=PROJECT_ROOT / "docs", metavar="DIR",
                        help="Generar la web pública (por defecto en docs/): último análisis semanal + estrategia y factores del oro recalculados")
    parser.add_argument("--list-profiles", action="store_true", help="Listar perfiles configurados y salir")
    parser.add_argument("--validate-config", action="store_true", help="Validar la configuración y salir")
    parser.add_argument("--history", type=int, nargs="?", const=20, metavar="N", help="Mostrar las últimas N decisiones guardadas")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args(argv)

def report_path(config: AppConfig, output_dir: Path | None, record: RunRecord, all_ids: list[str]) -> Path:
    directory = output_dir or (PROJECT_ROOT / config.report.output_dir)
    name = f"informe_{record.created_at[:10]}"
    if record.profile:
        name += f"_{record.profile}"
    elif sorted(record.assets_requested) != sorted(all_ids):
        name += "_" + "-".join(record.assets_requested)
    return directory / f"{name}.html"

def report_title(config: AppConfig, record: RunRecord) -> str:
    if not record.profile:
        return config.report.title
    profile = config.profiles.get(record.profile)
    return f"{config.report.title} · {(profile.title if profile and profile.title else record.profile)}"

def print_summary(record: RunRecord, path: Path) -> None:
    print(f"\nInforme{f' ({record.profile})' if record.profile else ''}: {path}")
    print(f"{'Activo':<22}{'Punt.':>6}  {'Acción':<18}{'Importe':>9}")
    for d in record.decisions:
        score = "—" if d.opportunity_score is None else str(d.opportunity_score)
        print(f"{d.name:<22}{score:>6}  {ACTION_LABELS[d.final_action]:<18}{d.amount_eur:>7.0f} €")
    budget = record.config_snapshot.get("rules", {}).get("tactical_budget_eur")
    print(f"Capital asignado: {record.total_extra_eur:.0f} €" + (f" de {budget:.0f} € de referencia" if budget else ""))
    if record.errors:
        print(f"Avisos: {len(record.errors)} (ver sección 'Datos faltantes' y {record.log_path})")

def render_saved(args: argparse.Namespace, config: AppConfig, settings: Settings, store: RunStore) -> int:
    record = store.load(args.render)
    target = report_path(config, args.output_dir, record, [a.id for a in config.assets if a.enabled])
    path, fallback = write_report(record, settings.templates_dir, target, report_title(config, record), record.config_snapshot["rules"])
    print(f"Informe regenerado: {path}")
    return EXIT_REPORT if fallback else EXIT_OK

def run_strategies(args: argparse.Namespace, config: AppConfig, settings: Settings, run_id: str) -> int:
    """Calcula solo las estrategias de tendencia y escribe su informe y su JSON de auditoría."""
    names = list(config.strategies) if args.strategy == "all" else [x.strip() for x in args.strategy.split(",") if x.strip()]
    unknown = [n for n in names if n not in config.strategies]
    if unknown or not names:
        logger.error("Estrategias desconocidas: %s. Disponibles: %s", unknown or names, sorted(config.strategies))
        return EXIT_CONFIG
    service = build_market_service(config, settings, use_cache=not args.no_cache)
    results = [build_trend_strategy(n, config.strategies[n], service) for n in names]
    generated_at = datetime.now().isoformat(timespec="seconds")
    directory = args.output_dir or (PROJECT_ROOT / config.report.output_dir)
    path = directory / f"estrategia_{generated_at[:10]}_{'-'.join(names)}.html"
    try:
        write_strategy_report(results, settings.templates_dir, path, "Seguimiento de tendencia", generated_at)
    except ReportError as exc:
        logger.error("%s", exc)
        return EXIT_REPORT
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    json_path = settings.runs_dir / f"strategy_{run_id}.json"
    json_path.write_text(json.dumps({"run_id": run_id, "generated_at": generated_at, "strategies": [r.model_dump() for r in results]},
                                    ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nInforme de estrategias: {path}\nAuditoría: {json_path}")
    for r in results:
        line = f"{r.name:<34}{TREND_STATE_LABELS[r.state]:<20}"
        if r.risk and not r.error:
            line += f"entrada {r.risk.entry_price:.2f} · stop {r.risk.active_stop:.2f} · máx. {r.risk.max_shares} partic."
        print(line + (f"  ({r.error})" if r.error else ""))
    return EXIT_DEGRADED if all(r.error for r in results) else EXIT_OK

def run_site(args: argparse.Namespace, config: AppConfig, settings: Settings) -> int:
    """Genera la web: sin LLM ni búsquedas. Recalcula las estrategias (oro, S&P 500) y los factores del oro (pensado para ejecutarse a diario)."""
    service = build_market_service(config, settings, use_cache=not args.no_cache)
    gold = next((n for n, s in config.strategies.items() if s.asset_id == GOLD_ASSET_ID and s.enabled), None)
    strategy = build_trend_strategy(gold, config.strategies[gold], service) if gold else None
    macro_cache = FileCache(settings.cache_dir / "macro", config.data.macro_cache_ttl_hours, enabled=not args.no_cache) \
        if config.data.macro_data else None
    factors = build_factor_study(service, macro_cache)
    sp500 = next((n for n, s in config.regime_strategies.items() if s.asset_id == SP500_ASSET_ID and s.enabled), None)
    regime = build_regime_strategy(sp500, config.regime_strategies[sp500], service) if sp500 else None
    try:
        paths = build_site(settings.runs_dir, settings.templates_dir, args.site, strategy, factors, sp500_regime=regime)
    except ReportError as exc:
        logger.error("%s", exc)
        return EXIT_REPORT
    print("Web generada: " + ", ".join(str(p) for p in paths))
    if strategy:
        print(f"Oro · tendencia: {TREND_STATE_LABELS[strategy.state]}{f' ({strategy.error})' if strategy.error else ''}")
    if regime:
        detail = regime.error or f"{regime.exposure}% (viernes {regime.decision_date}){' · TRASPASO' if regime.changed else ''}" + \
            ("" if regime.week_complete else f" · provisional {regime.provisional_exposure}%")
        print(f"S&P 500 · régimen: {detail}")
    if not factors.error:
        print(f"Oro · factores: {factors.favorable} favorecen, {factors.unfavorable} perjudican, {factors.neutral} sin efecto claro")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings()
    run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    if args.handoff_status:  # sin log ni configuración: solo lee ficheros
        print(json.dumps(handoff_status(args.handoff_status), ensure_ascii=False, indent=1))
        return EXIT_OK
    log_file = setup_logging(run_id, settings.logs_dir, args.log_level)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG

    if args.validate_config:
        print(f"Configuración válida: {len(config.assets)} activos, presupuesto puntual {config.rules.tactical_budget_eur:.0f} {config.report.currency}")
        return EXIT_OK
    if args.list_assets:
        for a in config.assets:
            print(f"{a.id:<14} {a.ticker:<10} {a.asset_class:<10} {'' if a.enabled else '(deshabilitado) '}{a.name}")
        return EXIT_OK
    if args.list_profiles:
        for name, p in config.profiles.items():
            ids = p.assets if p.assets != "all" else "todos los habilitados"
            print(f"{name:<16} {p.title or '':<28} {ids if isinstance(ids, str) else ', '.join(ids)}")
        return EXIT_OK

    store = RunStore(settings.runs_dir)
    if args.history is not None:
        for row in store.history(args.history):
            print("{:<20} {:<14} {:>4} {:<14} {:>6} €  {}".format(row[0], row[1], "—" if row[2] is None else row[2], row[3], f"{row[4]:.0f}", row[5]))
        return EXIT_OK
    if args.render:
        return render_saved(args, config, settings, store)
    if args.strategy:
        return run_strategies(args, config, settings, run_id)
    if args.site:
        return run_site(args, config, settings)

    asset_ids = [args.asset] if args.asset else ([x.strip() for x in args.assets.split(",") if x.strip()] if args.assets else None)
    try:
        profiles = config.resolve_profiles([x.strip() for x in args.profiles.split(",") if x.strip()]) if args.profiles else None
        if profiles:  # se analiza la unión una sola vez
            asset_ids = sorted({i for ids in profiles.values() for i in ids})
        options = RunOptions(asset_ids=asset_ids, mode=args.mode, skip_macro=args.skip_macro, skip_supervisor=args.skip_supervisor,
                             use_cache=not args.no_cache, handoff_dir=args.handoff, profiles=profiles)
        pipeline = Pipeline(config, settings, options, run_id)
    except (ValueError, ConfigError) as exc:  # p. ej. activo desconocido o carpeta handoff ajena
        logger.error("%s", exc)
        return EXIT_CONFIG

    logger.info("Inicio de ejecución: modo=%s activos=%s", options.mode, pipeline.record.assets_requested)
    try:
        records = asyncio.run(pipeline.run_all())
    except KeyboardInterrupt:
        logger.warning("Interrumpido por el usuario")
        return 130
    if pipeline.pending:
        print(f"\nHandoff: faltan {len(pipeline.pending)} respuesta(s): {', '.join(pipeline.pending)}")
        print(f"  Peticiones: {args.handoff / 'requests'}/<agente>.md")
        print(f"  Respuestas: {args.handoff / 'responses'}/<agente>.json")
        print("Cuando estén escritas, repite exactamente el mismo comando.")
        return EXIT_PENDING

    all_ids = [a.id for a in config.assets if a.enabled]
    any_fallback, results = False, []
    for record in records:
        record.log_path = str(log_file)
        target = report_path(config, args.output_dir, record, all_ids)
        path, fallback = write_report(record, settings.templates_dir, target, report_title(config, record), config.rules.model_dump())
        record.report_path = str(path)
        json_path = store.save(record)
        logger.info("Ejecución guardada en %s", json_path)
        print_summary(record, path)
        any_fallback |= fallback
        results.append({"profile": record.profile, "report": str(path), "run_json": str(json_path), "fallback": fallback,
                        "decisions": [{"asset_id": d.asset_id, "name": d.name, "score": d.opportunity_score,
                                       "action": d.final_action, "amount_eur": d.amount_eur} for d in record.decisions]})
    if args.handoff:
        (args.handoff / RESULT_FILE).write_text(json.dumps({"run_id": pipeline.record.run_id, "reports": results}, ensure_ascii=False, indent=1), encoding="utf-8")

    if any_fallback:
        return EXIT_REPORT
    return EXIT_DEGRADED if not any(s.prices for s in pipeline.record.snapshots) else EXIT_OK

if __name__ == "__main__":
    sys.exit(main())
