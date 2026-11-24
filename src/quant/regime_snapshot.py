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
    return (f"Decisión solo al cierre semanal. Emergencia: salir si el VIX cierra > {cfg.vix_emergency:g} con el cierre bajo la "
            f"SMA{cfg.sma_slow}s. Bajista: salir si el cierre está bajo la SMA{cfg.sma_slow}s y la media es más baja que hace "
            f"{cfg.slope_weeks} semanas (si la semana cae ≥ {cfg.panic_week_drop_pct:g}% o el RSI{cfg.rsi_window} < {cfg.panic_rsi:g}, "
            f"se aplaza al viernes siguiente). Entrada: cierre > SMA{cfg.sma_slow}s +{cfg.enter_band_pct:g}%.")


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


def build_checks(r: Readings, cfg: RegimeStrategyConfig, p: RegimeParams, below: int, exposure: int, deferred: bool) -> list[RegimeCheck]:
    slow = f"SMA{cfg.sma_slow}"
    slope_pct = (r.sma_slow / r.sma_slow_prev - 1) * 100
    vix_status = "na" if r.vix is None else ("alerta" if r.emergency(p) else ("aviso" if r.vix > p.vix_emergency else "ok"))
    checks = [
        RegimeCheck(
            id="emergency", label=f"1 · Emergencia: VIX al cierre semanal (con el cierre bajo la {slow}s)",
            value=_fmt(r.vix), rule=f"VIX > {cfg.vix_emergency:g} y cierre bajo la {slow}s → salir sin esperar",
            status=vix_status,
            note=("se cumple: salida de emergencia" if r.emergency(p) else
                  ("VIX alto, pero el cierre sigue sobre la media" if r.vix is not None and r.vix > p.vix_emergency else ""))),
        RegimeCheck(
            id="trend", label=f"2 · Bajista: cierre semanal frente a la {slow}s y su pendiente",
            value=f"{_fmt(r.close)} · {slow} {_fmt(r.sma_slow)} ({r.gap_pct:+.1f}%) · pendiente {slope_pct:+.2f}% en {cfg.slope_weeks} sem.",
            rule=f"Cierre bajo la {slow}s y media más baja que hace {cfg.slope_weeks} semanas → salir",
            status="alerta" if r.bear else ("aviso" if r.below else "ok"),
            note=(f"{below} cierre(s) semanal(es) seguido(s) bajo la {slow}" if below else "") +
                 (" · la media aún sube: no es bajista" if r.below and not r.slope_down else "")),
        RegimeCheck(
            id="panic", label=f"3 · Semana estirada: caída semanal y RSI{cfg.rsi_window} diario",
            value=f"{'—' if r.week_ret is None else f'{r.week_ret * 100:+.1f}%'} · RSI {_fmt(r.rsi, 0)}",
            rule=f"Caída ≥ {cfg.panic_week_drop_pct:g}% o RSI < {cfg.panic_rsi:g} → la salida bajista se aplaza al viernes siguiente (una vez)",
            status="aviso" if r.stretched(p) else "ok",
            note="salida aplazada: se decide el próximo viernes" if deferred else ""),
        RegimeCheck(
            id="entry", label=f"4 · Entrada: cierre frente a la {slow}s +{cfg.enter_band_pct:g}%",
            value=f"{_fmt(r.close)} / {_fmt(r.enter_line(p))}", rule=f"Fuera del mercado: cierre > {_fmt(r.enter_line(p))} → entrar",
            status=("ok" if r.close > r.enter_line(p) else "aviso") if exposure == 0 else ("ok" if r.close > r.sma_slow else "aviso"),
            note="" if exposure == 0 else "solo cuenta cuando se está fuera"),
        RegimeCheck(
            id="cross", label=f"Contexto · SMA{cfg.sma_fast}s frente a {slow}s",
            value=f"{_fmt(r.sma_fast)} / {_fmt(r.sma_slow)}", rule="Solo contexto: el cruce confirma el régimen pero llega tarde",
            status="ok" if r.sma_fast > r.sma_slow else "aviso"),
        RegimeCheck(
            id="dd", label=f"Contexto · días de distribución en {cfg.dd_window} sesiones",
            value=str(r.dd25), rule=f"Solo contexto · ≤ {cfg.dd_aligned_max} sano · ≥ {cfg.dd_reduce} vigilar (caída ≥ {cfg.dd_drop_pct:g}% con más volumen)",
            status="ok" if r.dd25 <= p.dd_aligned_max else ("alerta" if r.dd25 >= p.dd_reduce else "aviso")),
        RegimeCheck(
            id="cmf", label=f"Contexto · Chaikin Money Flow ({cfg.cmf_window} sesiones)",
            value=f"{r.cmf:+.3f}", rule="Solo contexto · > 0 entrada de dinero · < 0 salida",
            status="ok" if r.cmf > 0 else "aviso"),
        RegimeCheck(
            id="obv", label=f"Contexto · divergencia OBV a {cfg.obv_weeks} semanas",
            value="Sí" if r.obv_div else "No", rule=f"Solo contexto · precio más alto que hace {cfg.obv_weeks} semanas con OBV más bajo",
            status="alerta" if r.obv_div else "ok"),
    ]
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
    if res.deferred:
        lines.append(f"Salida bajista aplazada por semana de pánico: el próximo viernes se sale si el cierre sigue bajo la SMA{cfg.sma_slow}s "
                     "y la media sigue bajando.")
    if res.exposure == 100:
        lines.append(f"Se saldría con un cierre semanal bajo la SMA{cfg.sma_slow}s ({_fmt(res.sma_slow)}) si la media baja, o de inmediato "
                     f"si además el VIX cierra la semana por encima de {cfg.vix_emergency:g}.")
    else:
        lines.append(f"Se volvería a entrar con un cierre semanal por encima de {_fmt(res.enter_line)} "
                     f"({cfg.enter_band_pct:g}% sobre la SMA{cfg.sma_slow}s, que está en {_fmt(res.sma_slow)}).")
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
            logger.warning("Sin %s para %s: la salida de emergencia queda desactivada (%s)", cfg.vix_ticker, strategy_id, exc)
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
        need = cfg.sma_slow + cfg.slope_weeks
        return base.model_copy(update={"audit": audit, "error": f"Historia insuficiente: {len(weekly)} semanas (se necesitan al menos {need})"})

    last_switch = rep.switches[-1] if rep.switches else None
    changed = last_switch is not None and last_switch.date == rep.last_decision
    provisional, provisional_reasons = rep.exposure, []
    if not complete:
        provisional, _, provisional_reasons = decide(rep.exposure, rep.deferred, r, p)

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
        enter_line=[round_or_none(v * (1 + p.enter_band_pct / 100), 2) for v in tail["sma_slow"]],
        exposure=[None if pd.isna(v) else int(v) for v in tail["exposure"]], dd25=[None if pd.isna(v) else int(v) for v in tail["dd25"]],
    )
    result = base.model_copy(update=dict(
        exposure=rep.exposure, decision_date=rep.last_decision.strftime("%Y-%m-%d"), changed=changed,
        previous_exposure=last_switch.from_pct if changed else rep.exposure,
        provisional_exposure=provisional, provisional_reasons=provisional_reasons, week_complete=complete,
        last_date=daily.index[-1].strftime("%Y-%m-%d"), last_close=round(float(daily["close"].iloc[-1]), 2),
        sma_fast=round(r.sma_fast, 2), sma_slow=round(r.sma_slow, 2), sma_slow_prev=round(r.sma_slow_prev, 2),
        enter_line=round(r.enter_line(p), 2), gap_pct=round(r.gap_pct, 2),
        week_ret_pct=None if r.week_ret is None else round(r.week_ret * 100, 2), rsi=round_or_none(r.rsi, 1), deferred=rep.deferred, trend=trend_label(r), dd25=r.dd25, distribution_days=dd_days,
        cmf=round(r.cmf, 4), obv_divergence=r.obv_div, vix=round_or_none(r.vix, 2),
        checks=build_checks(r, cfg, p, weeks_below(weekly), rep.exposure, rep.deferred),
        switches=[RegimeSwitch(date=s.date.strftime("%Y-%m-%d"), from_pct=s.from_pct, to_pct=s.to_pct, reasons=s.reasons) for s in rep.switches],
        switches_per_year=round(len(rep.switches) / years, 1) if years > 0 else None, since=rep.first_valid.strftime("%Y-%m-%d"),
        perf={k: RegimePerf(**v) for k, v in perf.items()}, episodes=drawdown_episodes(daily, exposures, rep.first_valid),
        chart=chart, audit=audit,
    ))
    result.explanation = _explain(result, cfg)
    return result
