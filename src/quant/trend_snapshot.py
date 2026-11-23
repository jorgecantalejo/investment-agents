"""Construye el `TrendStrategyResult` de una estrategia: datos, reglas, riesgo, explicación y auditoría."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd

from src.config.models import TrendStrategyConfig
from src.core.errors import MarketDataError
from src.market.service import MarketDataService
from src.models import TrendAudit, TrendChart, TrendRisk, TrendStats, TrendStrategyResult
from src.quant.trend import TrendParams, indicators, position_size, replay, round_or_none

logger = logging.getLogger(__name__)


def params_from_config(cfg: TrendStrategyConfig) -> TrendParams:
    return TrendParams(
        filter_sma=cfg.filter_sma, entry_breakout=cfg.entry_breakout, exit_low=cfg.exit_low, atr_window=cfg.atr_window,
        atr_multiple=cfg.atr_multiple, capital=cfg.capital_eur, risk_pct=cfg.risk_pct, commission_pct=cfg.commission_pct,
        commission_min=cfg.commission_min_eur, spread_pct=cfg.spread_pct,
    )


def rule_text(cfg: TrendStrategyConfig) -> str:
    return (f"Filtro: cierre > SMA{cfg.filter_sma}. Entrada: cierre > máximo de las {cfg.entry_breakout} sesiones previas. "
            f"Stop inicial: entrada − {cfg.atr_multiple:g}×ATR{cfg.atr_window}. Salida: cierre ≤ stop o cierre < mínimo de las "
            f"{cfg.exit_low} sesiones previas. Señales al cierre; ejecución en la apertura siguiente.")


def drop_incomplete_bar(ohlc: pd.DataFrame, cfg: TrendStrategyConfig, now: datetime | None = None) -> tuple[pd.DataFrame, str | None]:
    """Quita la vela de hoy si la sesión aún no ha cerrado (Yahoo la publica en directo)."""
    now = now or datetime.now(ZoneInfo(cfg.exchange_tz))
    if ohlc.empty or ohlc.index[-1].date() != now.date():
        return ohlc, None
    if now.time() >= time.fromisoformat(cfg.session_close):
        return ohlc, None
    return ohlc.iloc[:-1], ohlc.index[-1].strftime("%Y-%m-%d")


def _fingerprint(ohlc: pd.DataFrame) -> str:
    return hashlib.sha256(ohlc.round(6).to_csv().encode("utf-8")).hexdigest()


def _fmt(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ")


def _explain(r: TrendStrategyResult, cfg: TrendStrategyConfig) -> list[str]:
    ccy = r.currency
    lines = []
    gap_sma = (r.last_close / r.sma - 1) * 100  # type: ignore[operator]
    if r.filter_pass:
        lines.append(f"Filtro superado: el cierre ({_fmt(r.last_close)} {ccy}) está un {gap_sma:.1f}% por encima de la SMA{cfg.filter_sma} "
                     f"({_fmt(r.sma)}). Solo con el filtro activo se buscan entradas.")  # type: ignore[arg-type]
    else:
        lines.append(f"Filtro no superado: el cierre ({_fmt(r.last_close)} {ccy}) está un {abs(gap_sma):.1f}% por debajo de la "
                     f"SMA{cfg.filter_sma} ({_fmt(r.sma)}). Aunque haya rupturas, no se abren posiciones nuevas.")  # type: ignore[arg-type]

    if r.state == "entrada_pendiente":
        lines.append(f"Señal de entrada: el cierre supera el máximo de las {cfg.entry_breakout} sesiones previas ({_fmt(r.entry_level)}). "
                     "La compra se haría en la apertura de la próxima sesión.")  # type: ignore[arg-type]
    elif r.state == "sin_posicion":
        if r.filter_pass:
            lines.append(f"Sin señal de entrada: para activarla, el cierre debe superar {_fmt(r.risk.entry_price)} "  # type: ignore[union-attr]
                         f"(máximo de {cfg.entry_breakout} sesiones), un {r.distance_to_entry_pct:.1f}% por encima del cierre actual.")  # type: ignore[arg-type]
        else:
            lines.append(f"Para activar una entrada, el cierre tendría que superar {_fmt(r.risk.entry_price)} (el mayor entre la "
                         f"SMA{cfg.filter_sma} y el máximo de {cfg.entry_breakout} sesiones), un {r.distance_to_entry_pct:.1f}% por encima del actual.")  # type: ignore[union-attr]
    elif r.position:
        pos = r.position
        gain = (r.last_close / pos.entry_price - 1) * 100  # type: ignore[operator]
        lines.append(f"Posición teórica abierta desde el {pos.entry_date} a {_fmt(pos.entry_price)} {ccy} ({gain:+.1f}%). "
                     f"Stop inicial {_fmt(pos.initial_stop)}; mínimo de {cfg.exit_low} sesiones {_fmt(r.exit_level)}.")  # type: ignore[arg-type]
        if r.state == "salida_pendiente":
            reason = "ha tocado el stop inicial" if r.last_close <= pos.initial_stop else \
                f"ha perdido el mínimo de las {cfg.exit_low} sesiones previas"  # type: ignore[operator]
            lines.append(f"Señal de salida: el cierre {reason}. La venta se haría en la apertura de la próxima sesión.")
        else:
            stop = r.risk.active_stop if r.risk else pos.initial_stop
            lines.append(f"Mantener mientras el cierre no baje de {_fmt(stop)} (el más alto entre el stop inicial y el mínimo de "
                         f"{cfg.exit_low} sesiones), un {(1 - stop / r.last_close) * 100:.1f}% por debajo del cierre.")  # type: ignore[operator]

    if r.atr is not None:
        lines.append(f"Volatilidad: ATR{cfg.atr_window} de {_fmt(r.atr)} {ccy} ({r.atr_pct:.1f}% del precio). "
                     f"El stop se sitúa a {cfg.atr_multiple:g} ATR para que el ruido diario normal no lo active.")
    if r.risk and r.risk.max_shares == 0:
        lines.append("Con este capital y este riesgo no cabe ni una participación: revisa capital_eur o risk_pct.")
    return lines


def build_trend_strategy(strategy_id: str, cfg: TrendStrategyConfig, service: MarketDataService,
                         now: datetime | None = None) -> TrendStrategyResult:
    """Resultado de una estrategia. Nunca lanza: los fallos quedan en `error` con estado insufficient_data."""
    base = TrendStrategyResult(id=strategy_id, name=cfg.name, ticker=cfg.ticker, isin=cfg.isin, currency=cfg.currency, asset_id=cfg.asset_id)
    try:
        data = service.get_ohlc(cfg.ticker, cfg.history_period)
    except MarketDataError as exc:
        logger.error("Sin OHLC para la estrategia %s (%s): %s", strategy_id, cfg.ticker, exc)
        return base.model_copy(update={"error": str(exc)})
    try:
        return _evaluate(base, cfg, data.ohlc, data.from_stale_cache, service.prices.name, now)
    except Exception as exc:  # un cálculo inesperado no debe tumbar el informe
        logger.exception("Error calculando la estrategia %s", strategy_id)
        return base.model_copy(update={"error": f"Error interno: {exc}"})


def _evaluate(base: TrendStrategyResult, cfg: TrendStrategyConfig, ohlc: pd.DataFrame, stale: bool, provider: str,
              now: datetime | None) -> TrendStrategyResult:
    p = params_from_config(cfg)
    ohlc, dropped = drop_incomplete_bar(ohlc, cfg, now)
    generated_at = (now or datetime.now(ZoneInfo(cfg.exchange_tz))).isoformat(timespec="seconds")
    audit = TrendAudit(
        rule=rule_text(cfg), provider=provider, ticker=cfg.ticker, period=cfg.history_period,
        first_date=ohlc.index[0].strftime("%Y-%m-%d") if len(ohlc) else "", last_date=ohlc.index[-1].strftime("%Y-%m-%d") if len(ohlc) else "",
        observations=len(ohlc), data_sha256=_fingerprint(ohlc), stale_cache=stale, dropped_incomplete_bar=dropped,
        generated_at=generated_at, params=cfg.model_dump(exclude={"name", "enabled"}),
    )
    frame = indicators(ohlc, p)
    if len(frame) == 0 or frame[["sma", "upper", "lower", "atr"]].iloc[-1].isna().any():
        need = max(cfg.filter_sma, cfg.entry_breakout + 1, cfg.atr_window)
        return base.model_copy(update={"audit": audit, "error": f"Historia insuficiente: {len(frame)} sesiones (se necesitan al menos {need})"})

    last = frame.iloc[-1]
    close = float(last["close"])
    rep = replay(frame, p)
    position = rep.trades[-1] if rep.trades and rep.trades[-1].is_open else None
    if position is None:
        state = "entrada_pendiente" if rep.pending_entry else "sin_posicion"
    else:
        state = "salida_pendiente" if rep.pending_exit else "mantener"

    atr = float(last["atr"])
    trigger = max(float(last["upper"]), float(last["sma"]))  # cierre necesario para cumplir filtro y ruptura a la vez
    if position is not None:
        entry, initial = position.entry_price, position.initial_stop
        active = max(initial, float(last["lower"]))
        shares, costs, capped = position_size(entry, initial, p)  # mismas cuentas que al abrirla
        basis = "en_curso"
    else:
        entry = close if state == "entrada_pendiente" else trigger
        initial = active = entry - p.atr_multiple * atr
        shares, costs, capped = position_size(entry, initial, p)
        basis = "prevista" if state == "entrada_pendiente" else "hipotetica"
    risk = TrendRisk(
        basis=basis, entry_price=round(entry, 4), initial_stop=round(initial, 4), active_stop=round(active, 4),  # type: ignore[arg-type]
        risk_per_share=round(entry - initial, 4), risk_budget=round(p.risk_budget, 2), max_shares=shares,
        position_value=round(shares * entry, 2), costs=costs, total_risk=round(shares * (entry - initial) + costs, 2), capped_by_capital=capped,
    )

    closed = [t for t in rep.trades if not t.is_open and t.r_multiple is not None]
    rs = [t.r_multiple for t in closed]
    valid_sessions = len(frame.loc[rep.first_valid:]) if rep.first_valid is not None else 0
    stats = TrendStats(
        trades=len(rep.trades), closed=len(closed),
        win_rate_pct=round(sum(r > 0 for r in rs) / len(rs) * 100, 1) if rs else None,  # type: ignore[operator]
        avg_r=round(sum(rs) / len(rs), 2) if rs else None,  # type: ignore[arg-type]
        total_r=round(sum(rs), 2) if rs else None,  # type: ignore[arg-type]
        best_r=max(rs) if rs else None, worst_r=min(rs) if rs else None,  # type: ignore[type-var]
        exposure_pct=round(rep.sessions_in_market / valid_sessions * 100, 1) if valid_sessions else None,
        since=rep.first_valid.strftime("%Y-%m-%d") if rep.first_valid is not None else None,
    )
    tail = frame.iloc[-cfg.chart_sessions:]
    chart = TrendChart(
        dates=[d.strftime("%Y-%m-%d") for d in tail.index], close=[round(float(v), 4) for v in tail["close"]],
        sma=[round_or_none(v) for v in tail["sma"]], upper=[round_or_none(v) for v in tail["upper"]], lower=[round_or_none(v) for v in tail["lower"]],
    )
    upper = float(last["upper"])
    result = base.model_copy(update=dict(
        state=state, last_date=frame.index[-1].strftime("%Y-%m-%d"), last_close=round(close, 4), sma=round(float(last["sma"]), 4),
        filter_pass=close > float(last["sma"]), entry_level=round(upper, 4), entry_signal=close > upper and close > float(last["sma"]),
        distance_to_entry_pct=round((trigger / close - 1) * 100, 2), exit_level=round(float(last["lower"]), 4),
        atr=round(atr, 4), atr_pct=round(atr / close * 100, 2), position=position, risk=risk,
        trades=rep.trades, stats=stats, chart=chart, audit=audit,
    ))
    result.explanation = _explain(result, cfg)
    return result
