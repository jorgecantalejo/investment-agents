"""Construye el `RegimeStrategyResult` del S&P 500: datos, checklist del viernes, decisión, histórico y comparativa."""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from src.config.models import RegimeStrategyConfig
from src.core.errors import MarketDataError
from src.market.service import MarketDataService
from src.models import FlowDay, RegimeChart, RegimeCheck, RegimePerf, RegimeStrategyResult, RegimeSwitch, TrendAudit
from src.quant.regime import (
    Readings, RegimeParams, daily_indicators, decide, performance, readings_at, replay, sma_only_exposure, weekly_frame,
)
from src.quant.trend import round_or_none
from src.quant.trend_snapshot import drop_incomplete_bar

logger = logging.getLogger(__name__)

EPISODE_MIN_DRAWDOWN_PCT = 15.0
MAX_EPISODES = 6


def params_from_config(cfg: RegimeStrategyConfig) -> RegimeParams:
    fields = RegimeParams.__dataclass_fields__
    return RegimeParams(**{k: v for k, v in cfg.model_dump().items() if k in fields})


def rule_text(cfg: RegimeStrategyConfig) -> str:
    return (f"Decisión solo al cierre semanal. 100% si cierre > SMA{cfg.sma_slow}s, SMA{cfg.sma_fast}s > SMA{cfg.sma_slow}s, "
            f"DD{cfg.dd_window} ≤ {cfg.dd_aligned_max}, CMF{cfg.cmf_window} > 0, sin divergencia OBV y VIX ≤ {cfg.vix_max:g}. "
            f"50% si el cierre pierde la SMA{cfg.sma_slow}s o hay {cfg.warnings_to_reduce} avisos de flujo/macro. "
            f"0% con dos cierres semanales bajo la SMA{cfg.sma_slow}s confirmados por distribución, CMF o cruce de medias. "
            f"Reentrada al 50% con Follow-Through Day (día {cfg.ftd_first_day}-{cfg.ftd_last_day}, ≥ +{cfg.ftd_gain_pct:g}% con más volumen).")


def week_is_complete(last_bar: date, today: date) -> bool:
    """La semana de la última vela está cerrada si esa vela es de viernes o ya ha pasado su viernes."""
    friday = last_bar + pd.Timedelta(days=(4 - last_bar.weekday()) % 7)
    return last_bar.weekday() == 4 or today > friday


def _fmt(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:,.{digits}f}".replace(",", " ")


def _fingerprint(frame: pd.DataFrame) -> str:
    return hashlib.sha256(frame.round(6).to_csv().encode("utf-8")).hexdigest()


def trend_label(r: Readings) -> str:
    return "alcista" if r.trend_up else ("bajista" if r.trend_down else "transicion")


def weeks_below(weekly: pd.DataFrame) -> int:
    """Cierres semanales consecutivos bajo la media lenta, contando el último."""
    count = 0
    for close, sma in zip(reversed(weekly["close"].tolist()), reversed(weekly["sma_slow"].tolist())):
        if pd.isna(sma) or close >= sma:
            break
        count += 1
    return count


def build_checks(r: Readings, cfg: RegimeStrategyConfig, p: RegimeParams, below: int, exposure: int, rally) -> list[RegimeCheck]:
    gap = (r.close / r.sma_slow - 1) * 100
    trend_status = "ok" if r.trend_up else ("alerta" if r.trend_down or below >= 2 else "aviso")
    checks = [
        RegimeCheck(
            id="trend", label=f"1 · Tendencia: cierre semanal frente a SMA{cfg.sma_slow}s y cruce SMA{cfg.sma_fast}s/SMA{cfg.sma_slow}s",
            value=f"{_fmt(r.close)} · SMA{cfg.sma_fast} {_fmt(r.sma_fast)} · SMA{cfg.sma_slow} {_fmt(r.sma_slow)} ({gap:+.1f}%)",
            rule=f"Alcista: cierre > SMA{cfg.sma_slow} y SMA{cfg.sma_fast} > SMA{cfg.sma_slow}. Dos cierres seguidos bajo la SMA{cfg.sma_slow} = ruptura confirmada.",
            status=trend_status,
            note=(f"{below} cierre(s) semanal(es) seguido(s) bajo la SMA{cfg.sma_slow}" if below else
                  ("SMA rápida por encima de la lenta" if r.sma_fast > r.sma_slow else "SMA rápida aún por debajo de la lenta"))),
        RegimeCheck(
            id="dd", label=f"2 · Días de distribución en {cfg.dd_window} sesiones",
            value=str(r.dd25), rule=f"≤ {cfg.dd_aligned_max} flujo sano · ≥ {cfg.dd_reduce} aviso (caída ≥ {cfg.dd_drop_pct:g}% con más volumen)",
            status="ok" if r.dd25 <= p.dd_aligned_max else ("alerta" if r.dd25 >= p.dd_reduce else "aviso")),
        RegimeCheck(
            id="cmf", label=f"3 · Chaikin Money Flow ({cfg.cmf_window} sesiones)",
            value=f"{r.cmf:+.3f}", rule="> 0 entrada de dinero · < 0 dos viernes seguidos = aviso",
            status="ok" if r.cmf > 0 else ("alerta" if r.prev_cmf_negative else "aviso"),
            note="también negativo el viernes anterior" if r.cmf < 0 and r.prev_cmf_negative else ""),
        RegimeCheck(
            id="obv", label=f"4 · Divergencia OBV a {cfg.obv_weeks} semanas",
            value="Sí" if r.obv_div else "No", rule=f"Aviso si el precio está más alto que hace {cfg.obv_weeks} semanas y el OBV más bajo",
            status="alerta" if r.obv_div else "ok"),
        RegimeCheck(
            id="vix", label="5 · Tensión macro: VIX al cierre semanal",
            value=_fmt(r.vix), rule=f"≤ {cfg.vix_max:g} normal · > {cfg.vix_max:g} dos viernes seguidos = aviso",
            status="na" if r.vix is None else ("ok" if r.vix <= p.vix_max else ("alerta" if r.prev_vix_high else "aviso"))),
    ]
    if exposure == 0:
        if rally is None:
            value, status, note = "—", "na", ""
        elif rally.ftd_date is not None:
            value, status, note = f"FTD el {rally.ftd_date:%Y-%m-%d}", "ok", f"mínimo del intento {_fmt(rally.low)}: debe respetarse al cierre del viernes"
        elif rally.expired:
            value, status, note = "Intento caducado sin FTD", "aviso", f"se reinicia si el precio pierde {_fmt(rally.low)}"
        elif rally.day == 0:
            value, status, note = "Esperando el día 1", "aviso", f"mínimo de referencia {_fmt(rally.low)}"
        else:
            value, status, note = f"Día {rally.day} del intento", "aviso", f"FTD posible entre los días {cfg.ftd_first_day} y {cfg.ftd_last_day}; mínimo {_fmt(rally.low)}"
        checks.append(RegimeCheck(id="ftd", label="6 · Follow-Through Day (solo fuera del mercado)", value=value,
                                  rule=f"Día {cfg.ftd_first_day}-{cfg.ftd_last_day} del rebote, subida ≥ {cfg.ftd_gain_pct:g}% con volumen > sesión anterior",
                                  status=status, note=note))
    return checks


def drawdown_episodes(daily: pd.DataFrame, exposures: dict[str, pd.Series], since: pd.Timestamp) -> list[dict]:
    """Caídas de comprar y mantener ≥ 15% (pico → mínimo) y lo que hizo cada exposición en ese mismo tramo."""
    ret = daily["ret"].loc[since:].fillna(0.0)
    equity = (1 + ret).cumprod()
    peak_val, peak_date, trough_val, trough_date = equity.iloc[0], equity.index[0], equity.iloc[0], equity.index[0]
    episodes = []

    def close_episode() -> None:
        depth = (trough_val / peak_val - 1) * 100
        if depth <= -EPISODE_MIN_DRAWDOWN_PCT:
            episodes.append((peak_date, trough_date, depth))

    for ts, value in equity.items():
        if value > peak_val:
            close_episode()
            peak_val, peak_date, trough_val, trough_date = value, ts, value, ts
        elif value < trough_val:
            trough_val, trough_date = value, ts
    close_episode()
    out = []
    for start, end, depth in sorted(episodes, key=lambda e: e[2])[:MAX_EPISODES]:
        row = {"start": start.strftime("%Y-%m-%d"), "end": end.strftime("%Y-%m-%d"), "buy_hold": round(depth, 1)}
        window = ret.loc[start:end].iloc[1:]
        for name, exposure in exposures.items():
            held = exposure.shift(1).reindex(window.index).fillna(0.0)
            row[name] = round(float(((1 + held * window).prod() - 1) * 100), 1)
        out.append(row)
    return sorted(out, key=lambda e: e["start"])


def _explain(res: RegimeStrategyResult, cfg: RegimeStrategyConfig) -> list[str]:
    lines = []
    where = f"{res.exposure}% en el fondo S&P 500 y {100 - res.exposure}% en el monetario"  # type: ignore[operator]
    if res.changed:
        lines.append(f"Decisión del viernes {res.decision_date}: pasar del {res.previous_exposure}% al {res.exposure}% → ordenar el traspaso "
                     f"para quedar con {where}.")
    else:
        lines.append(f"Decisión del viernes {res.decision_date}: mantener {where}. No hay que hacer ningún traspaso.")
    if res.switches and res.changed:
        lines.append("Motivo: " + "; ".join(res.switches[-1].reasons) + ".")
    if res.mode == "rebote":
        lines.append(f"El 50% actual es de rebote (tras un Follow-Through Day): si un cierre semanal pierde {_fmt(res.rally_low)}, se vuelve al 0%.")
    if res.shock_week:
        lines.append(f"Semana de choque: la última sesión cayó más de un {cfg.shock_day_pct:g}% y se ha evaluado con el cierre anterior; "
                     "el desplome cuenta la semana que viene, cuando se vea si hubo rebote.")
    if not res.week_complete:
        if res.provisional_exposure != res.exposure:
            lines.append(f"Lectura provisional a {res.last_date} (la semana aún no ha cerrado): si el viernes cerrara así, la regla pasaría "
                         f"al {res.provisional_exposure}% ({'; '.join(res.provisional_reasons)}). No se actúa hasta el cierre del viernes.")
        else:
            lines.append(f"Lectura provisional a {res.last_date}: con los datos de hoy la regla mantendría el {res.exposure}%. Se decide al cierre del viernes.")
    return lines


def build_regime_strategy(strategy_id: str, cfg: RegimeStrategyConfig, service: MarketDataService,
                          now: datetime | None = None) -> RegimeStrategyResult:
    """Resultado de la estrategia. Nunca lanza: los fallos quedan en `error`."""
    base = RegimeStrategyResult(id=strategy_id, name=cfg.name, ticker=cfg.ticker, vehicle=cfg.vehicle, asset_id=cfg.asset_id)
    try:
        data = service.get_ohlcv(cfg.ticker, cfg.history_period)
    except MarketDataError as exc:
        logger.error("Sin velas con volumen para %s (%s): %s", strategy_id, cfg.ticker, exc)
        return base.model_copy(update={"error": str(exc)})
    vix = None
    if cfg.vix_ticker:
        try:
            vix = service.get_ohlc(cfg.vix_ticker, cfg.history_period).ohlc["close"]
        except MarketDataError as exc:
            logger.warning("Sin %s para %s: la tensión macro queda sin dato (%s)", cfg.vix_ticker, strategy_id, exc)
    try:
        return _evaluate(base, cfg, data.ohlc, vix, data.from_stale_cache, service.prices.name, now)
    except Exception as exc:  # un cálculo inesperado no debe tumbar la web
        logger.exception("Error calculando la estrategia %s", strategy_id)
        return base.model_copy(update={"error": f"Error interno: {exc}"})


def _evaluate(base: RegimeStrategyResult, cfg: RegimeStrategyConfig, ohlcv: pd.DataFrame, vix: pd.Series | None, stale: bool,
              provider: str, now: datetime | None) -> RegimeStrategyResult:
    p = params_from_config(cfg)
    now = now or datetime.now(ZoneInfo(cfg.exchange_tz))
    ohlcv, dropped = drop_incomplete_bar(ohlcv, cfg, now)  # type: ignore[arg-type]
    audit = TrendAudit(
        rule=rule_text(cfg), provider=provider, ticker=cfg.ticker, period=cfg.history_period,
        first_date=ohlcv.index[0].strftime("%Y-%m-%d") if len(ohlcv) else "", last_date=ohlcv.index[-1].strftime("%Y-%m-%d") if len(ohlcv) else "",
        observations=len(ohlcv), data_sha256=_fingerprint(ohlcv), stale_cache=stale, dropped_incomplete_bar=dropped,
        generated_at=now.isoformat(timespec="seconds"), params=cfg.model_dump(exclude={"name", "enabled", "vehicle"}),
    )
    if len(ohlcv) < 5:
        return base.model_copy(update={"audit": audit, "error": f"Historia insuficiente: {len(ohlcv)} sesiones"})
    complete = week_is_complete(ohlcv.index[-1].date(), now.date())
    daily = daily_indicators(ohlcv, vix, p)
    weekly = weekly_frame(daily, p, last_week_complete=complete)
    rep = replay(daily, weekly, p)
    r = readings_at(weekly, len(weekly) - 1, p)
    if rep.first_valid is None or r is None or rep.last_decision is None:
        need = max(cfg.sma_slow + 1, 2)
        return base.model_copy(update={"audit": audit, "error": f"Historia insuficiente: {len(weekly)} semanas (se necesitan al menos {need})"})

    last_switch = rep.switches[-1] if rep.switches else None
    changed = last_switch is not None and last_switch.date == rep.last_decision
    provisional, provisional_reasons = rep.exposure, []
    if not complete:
        ftd_ok = rep.ftd_pending is not None and rep.rally is not None and rep.rally.ftd_date is not None and r.close > rep.rally.low
        provisional, _, provisional_reasons = decide(rep.exposure, rep.mode, r, ftd_ok, rep.rally_low, p)

    window = daily.iloc[-cfg.dd_window:]
    dd_days = [FlowDay(date=ts.strftime("%Y-%m-%d"), change_pct=round(float(row["ret"]) * 100, 2),
                       volume_change_pct=round(float(row["volume"] / daily["volume"].shift(1).loc[ts] - 1) * 100, 1))
               for ts, row in window[window["dd"]].iterrows()]

    exposures = {"strategy": rep.daily_exposure, "sma_only": sma_only_exposure(rep.weekly, daily.index, p)}
    perf = performance(daily, exposures, p, rep.first_valid)
    years = (daily.index[-1] - rep.first_valid).days / 365.25
    tail = rep.weekly.iloc[-cfg.chart_weeks:]
    chart = RegimeChart(
        dates=[d.strftime("%Y-%m-%d") for d in tail.index], close=[round(float(v), 2) for v in tail["close"]],
        sma_fast=[round_or_none(v, 2) for v in tail["sma_fast"]], sma_slow=[round_or_none(v, 2) for v in tail["sma_slow"]],
        exposure=[None if pd.isna(v) else int(v) for v in tail["exposure"]], dd25=[None if pd.isna(v) else int(v) for v in tail["dd25"]],
    )
    rally = rep.rally if rep.exposure == 0 else None
    last_week = weekly.iloc[-1]
    result = base.model_copy(update=dict(
        exposure=rep.exposure, mode=rep.mode, decision_date=rep.last_decision.strftime("%Y-%m-%d"), changed=changed,
        previous_exposure=last_switch.from_pct if changed else rep.exposure,
        provisional_exposure=provisional, provisional_reasons=provisional_reasons, week_complete=complete,
        shock_week=bool(last_week["shock"]), last_date=daily.index[-1].strftime("%Y-%m-%d"), last_close=round(float(daily["close"].iloc[-1]), 2),
        sma_fast=round(r.sma_fast, 2), sma_slow=round(r.sma_slow, 2), trend=trend_label(r), dd25=r.dd25, distribution_days=dd_days,
        cmf=round(r.cmf, 4), obv_divergence=r.obv_div, vix=round_or_none(r.vix, 2),
        rally_day=rally.day if rally else None, rally_low=round_or_none(rep.rally_low if rep.mode == "rebote" else (rally.low if rally else None), 2),
        ftd_date=rally.ftd_date.strftime("%Y-%m-%d") if rally and rally.ftd_date is not None else None,
        checks=build_checks(r, cfg, p, weeks_below(weekly), rep.exposure, rally),
        switches=[RegimeSwitch(date=s.date.strftime("%Y-%m-%d"), from_pct=s.from_pct, to_pct=s.to_pct, reasons=s.reasons) for s in rep.switches],
        switches_per_year=round(len(rep.switches) / years, 1) if years > 0 else None, since=rep.first_valid.strftime("%Y-%m-%d"),
        perf={k: RegimePerf(**v) for k, v in perf.items()}, episodes=drawdown_episodes(daily, exposures, rep.first_valid),
        chart=chart, audit=audit,
    ))
    result.explanation = _explain(result, cfg)
    return result
