"""Estrategia de seguimiento de tendencia: indicadores, reconstrucción de operaciones, tamaño y estado."""

from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from src.config import parse_config
from src.config.models import TrendStrategyConfig
from src.core.errors import InvalidTickerError
from src.market.service import OhlcResult
from src.quant.trend import TrendParams, average_true_range, indicators, position_size, replay, side_cost
from src.quant.trend_snapshot import build_trend_strategy, drop_incomplete_bar


def ohlc_from_close(close: list[float], spread: float = 0.5, start: str = "2024-01-01") -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=len(close))
    c = pd.Series(close, index=idx, dtype=float)
    return pd.DataFrame({"open": c.shift(1).fillna(c.iloc[0]), "high": c + spread, "low": c - spread, "close": c})


class FakeService:
    class prices:
        name = "fake"

    def __init__(self, ohlc=None, error=None):
        self.ohlc, self.error = ohlc, error

    def get_ohlc(self, ticker, period=None):
        if self.error:
            raise self.error
        return OhlcResult(self.ohlc)


SMALL = dict(filter_sma=20, entry_breakout=5, exit_low=3, atr_window=5)
AFTER_CLOSE = datetime(2030, 1, 1, 20, 0, tzinfo=ZoneInfo("Europe/Berlin"))


def strategy_cfg(**overrides) -> TrendStrategyConfig:
    return TrendStrategyConfig(name="Oro", ticker="PPFB.DE", **{**SMALL, **overrides})


def test_atr_constant_range():
    df = ohlc_from_close([100.0] * 30, spread=1.0)
    atr = average_true_range(df, 5)
    assert atr.iloc[:4].isna().all()
    assert atr.iloc[-1] == pytest.approx(2.0)


def test_channel_uses_previous_sessions_only():
    df = ohlc_from_close(list(np.arange(100, 130, dtype=float)), spread=0)
    frame = indicators(df, TrendParams(**SMALL))
    # El máximo previo de 5 sesiones en la última fila es el cierre de ayer, no el de hoy.
    assert frame["upper"].iloc[-1] == df["high"].iloc[-2]
    assert frame["lower"].iloc[-1] == df["low"].iloc[-4]


def test_position_size_respects_risk_budget_with_costs():
    p = TrendParams(capital=10000, risk_pct=1.0, commission_pct=0.1, spread_pct=0.05)
    shares, costs, capped = position_size(entry=70.0, stop=67.5, p=p)
    assert shares * 2.5 + costs <= 100
    assert (shares + 1) * 2.5 + side_cost((shares + 1) * 70, p) + side_cost((shares + 1) * 67.5, p) > 100
    assert not capped


def test_position_size_capped_by_capital():
    p = TrendParams(capital=1000, risk_pct=5.0, commission_pct=0, spread_pct=0)
    shares, _, capped = position_size(entry=100.0, stop=99.9, p=p)
    assert shares == 10 and capped


def test_position_size_invalid_stop():
    assert position_size(100.0, 100.0, TrendParams())[0] == 0


def test_replay_enters_next_open_and_exits_on_low_break():
    close = [100.0] * 25 + [101, 102, 103, 104, 105, 106, 107, 108, 100, 99, 98]
    frame = indicators(ohlc_from_close(close, spread=0.2), TrendParams(**SMALL))
    rep = replay(frame, TrendParams(**SMALL))
    first = rep.trades[0]
    signal_pos = frame.index.get_loc(pd.Timestamp(first.signal_date))
    assert frame.index[signal_pos + 1].strftime("%Y-%m-%d") == first.entry_date
    assert first.entry_price == frame["open"].iloc[signal_pos + 1]
    assert first.exit_reason in ("stop", "minimo")
    assert first.exit_date is not None


def test_state_entrada_pendiente_on_last_bar_breakout():
    close = [100.0] * 30 + [99.0] * 5 + [103.0]
    result = build_trend_strategy("g", strategy_cfg(), FakeService(ohlc_from_close(close, spread=0.2)), now=AFTER_CLOSE)
    assert result.state == "entrada_pendiente"
    assert result.filter_pass and result.entry_signal
    assert result.risk.basis == "prevista"
    assert result.risk.entry_price == 103.0
    assert result.risk.initial_stop == pytest.approx(103.0 - 2 * result.atr, abs=1e-3)
    assert result.risk.total_risk <= result.risk.risk_budget


def test_state_mantener_and_salida_pendiente():
    up = [100.0] * 25 + [100 + i for i in range(1, 15)]
    hold = build_trend_strategy("g", strategy_cfg(), FakeService(ohlc_from_close(up, spread=0.2)), now=AFTER_CLOSE)
    assert hold.state == "mantener" and hold.position is not None
    assert hold.risk.basis == "en_curso" and hold.risk.active_stop >= hold.risk.initial_stop

    down = build_trend_strategy("g", strategy_cfg(), FakeService(ohlc_from_close(up + [105.0], spread=0.2)), now=AFTER_CLOSE)
    assert down.state == "salida_pendiente"


def test_state_sin_posicion_below_filter_uses_max_of_sma_and_breakout():
    close = [120.0] * 25 + [100.0] * 10
    result = build_trend_strategy("g", strategy_cfg(), FakeService(ohlc_from_close(close, spread=0.2)), now=AFTER_CLOSE)
    assert result.state == "sin_posicion" and result.filter_pass is False
    assert result.risk.basis == "hipotetica"
    assert result.risk.entry_price == pytest.approx(max(result.sma, result.entry_level))
    assert any("Filtro no superado" in line for line in result.explanation)


def test_insufficient_history_and_provider_error():
    short = build_trend_strategy("g", strategy_cfg(), FakeService(ohlc_from_close([100.0] * 10)), now=AFTER_CLOSE)
    assert short.state == "insufficient_data" and "Historia insuficiente" in short.error
    broken = build_trend_strategy("g", strategy_cfg(), FakeService(error=InvalidTickerError("sin datos")))
    assert broken.state == "insufficient_data" and broken.error == "sin datos"


def test_audit_fields():
    result = build_trend_strategy("g", strategy_cfg(), FakeService(ohlc_from_close([100.0] * 40)), now=AFTER_CLOSE)
    assert result.audit.observations == 40
    assert len(result.audit.data_sha256) == 64
    assert "SMA20" in result.audit.rule and result.audit.params["risk_pct"] == 1.0


def test_drop_incomplete_bar_only_during_session():
    df = ohlc_from_close([100.0] * 3, start="2026-11-18")  # 18, 19 y 20 de noviembre
    tz = ZoneInfo("Europe/Berlin")
    cfg = strategy_cfg()
    trimmed, dropped = drop_incomplete_bar(df, cfg, datetime(2026, 11, 20, 12, 0, tzinfo=tz))
    assert len(trimmed) == 2 and dropped == "2026-11-20"
    kept, none = drop_incomplete_bar(df, cfg, datetime(2026, 11, 20, 18, 0, tzinfo=tz))
    assert len(kept) == 3 and none is None


def test_config_validation(minimal_config_dict):
    ok = parse_config({**minimal_config_dict, "strategies": {"gold_trend": {"name": "Oro", "ticker": "PPFB.DE", "asset_id": "gold"}}})
    assert list(ok.strategies_for(["gold"])) == ["gold_trend"]
    assert ok.strategies_for(["msci_world"]) == {}
    with pytest.raises(Exception, match="activo desconocido"):
        parse_config({**minimal_config_dict, "strategies": {"x": {"name": "X", "ticker": "T", "asset_id": "nope"}}})
    with pytest.raises(Exception, match="report.currency"):
        parse_config({**minimal_config_dict, "strategies": {"x": {"name": "X", "ticker": "T", "currency": "USD"}}})
    with pytest.raises(Exception, match="exit_low"):
        parse_config({**minimal_config_dict, "strategies": {"x": {"name": "X", "ticker": "T", "exit_low": 30}}})


def test_render_in_report_and_standalone(run_record, tmp_path):
    from pathlib import Path

    from src.reporting.html import render_report, write_strategy_report

    templates = Path(__file__).resolve().parent.parent / "templates"
    up = [100.0] * 25 + [100 + i for i in range(1, 15)]
    strat = build_trend_strategy("gold_trend", strategy_cfg(), FakeService(ohlc_from_close(up, spread=0.2)), now=AFTER_CLOSE)
    run_record.strategies = [strat]
    html = render_report(run_record, templates, "Informe", {"tactical_budget_eur": 300})
    assert 'id="estrategias"' in html and "Mantener" in html and "Tamaño máximo permitido" in html
    path = write_strategy_report([strat], templates, tmp_path / "e.html", "Seguimiento de tendencia", "2030-01-01T20:00:00")
    text = path.read_text(encoding="utf-8")
    assert "Cómo funciona" in text and "<svg class=\"tchart\"" in text and "Auditoría" in text

    run_record.strategies = []
    assert 'id="estrategias"' not in render_report(run_record, templates, "Informe", {"tactical_budget_eur": 300})
