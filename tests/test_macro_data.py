"""Series macro de FRED y CAPE de Shiller: parseo, cálculos y paso por el pipeline (sin red)."""

import asyncio

import pandas as pd
import pytest

from src import pipeline as pipeline_mod
from src.core.errors import NetworkError
from src.market.providers import cape, fred
from src.pipeline import Pipeline, RunOptions
from src.quant.indicators import cape_stats, fred_values
from src.settings import Settings
from tests.conftest import make_analysis
from tests.test_pipeline import FakePrices


def test_fred_csv_skips_gaps():
    series = fred.parse_csv("observation_date,DFF\n2026-11-01,3.88\n2026-11-02,.\n2026-11-03,3.87\n", "DFF")
    assert list(series) == [3.88, 3.87]
    with pytest.raises(NetworkError):
        fred.parse_csv("observation_date,OTRA\n2026-11-01,1\n", "DFF")


def test_fred_values_level_and_yoy():
    daily = pd.Series(range(800), index=pd.bdate_range(end="2026-11-20", periods=800), dtype=float)
    level = fred_values(daily, "level")
    assert level["value"] == 799 and level["as_of"] == "2026-11-20"
    assert level["previous"]["1m"] == daily.asof(pd.Timestamp("2026-10-20"))

    cpi = pd.Series([100 * 1.03 ** (i / 12) for i in range(30)], index=pd.date_range(end="2026-10-01", periods=30, freq="MS"))
    yoy = fred_values(cpi, "yoy")
    assert yoy["value"] == pytest.approx(3.0, abs=0.01) and yoy["as_of"] == "2026-10-01"
    assert yoy["previous"]["12m"] == pytest.approx(3.0, abs=0.01)


def _cape_rows(n_months: int = 600, last: float = 36.0) -> list[tuple[str, float]]:
    dates = pd.date_range(end="2026-11-01", periods=n_months, freq="MS")
    values = [10 + 20 * i / n_months for i in range(n_months - 1)] + [last]
    return [(d.strftime("%Y-%m-%d"), v) for d, v in zip(dates, values)]


def test_cape_table_parsing():
    rows = "".join(f'<tr class="odd">\n<td>{pd.Timestamp(d):%b} {pd.Timestamp(d).day}, {pd.Timestamp(d).year}</td>\n<td>\n&#x2002;\n{v:.2f}\n</td>\n</tr>'
                   for d, v in reversed(_cape_rows(150)))
    parsed = cape.parse_table(f'<table id="datatable"><tr><th>Date</th><th>Value</th></tr>{rows}</table>')
    assert len(parsed) == 150 and parsed[-1] == ("2026-11-01", 36.0) and parsed[0][0] < parsed[-1][0]
    with pytest.raises(NetworkError):
        cape.parse_table("<html>cambio de formato</html>")


def test_cape_stats_percentiles():
    stats = cape_stats(_cape_rows(600, last=36.0))
    assert stats.value == 36.0 and stats.as_of == "2026-11-01"
    assert stats.percentile_all == 100.0 and stats.percentile_30y == 100.0
    low = cape_stats(_cape_rows(600, last=5.0))
    assert low.percentile_all == pytest.approx(100 / 600, abs=0.1)
    assert stats.median_30y > stats.median_all  # la serie sintética crece con el tiempo


def test_pipeline_uses_fred_and_cape(monkeypatch, tmp_path, minimal_config_dict):
    from src.config import parse_config

    minimal_config_dict["assets"].append({"id": "sp500", "name": "S&P 500", "ticker": "^GSPC", "asset_class": "equity", "cape_reference": "sp500"})
    config = parse_config(minimal_config_dict)
    monkeypatch.setattr(pipeline_mod, "YFinanceProvider", FakePrices)
    monkeypatch.setattr(cape, "fetch_shiller_cape", lambda: _cape_rows(600, last=36.0))
    monkeypatch.setattr(fred, "fetch_series", lambda series_id, start: pd.Series(
        [2.0 + i / 100 for i in range(40)], index=pd.date_range(end="2026-11-01", periods=40, freq="MS")))

    async def fake_run(self, agent, prompt, output_type, label):
        assert "CAPE" in prompt
        return make_analysis(label, valuation="cheap") if label == "sp500" else None

    monkeypatch.setattr(pipeline_mod.AnthropicRunner, "run", fake_run)
    settings = Settings(openai_api_key=None, tavily_api_key=None, openai_model=None, project_root=tmp_path)
    record = asyncio.run(Pipeline(config, settings, RunOptions(mode="no-web", skip_macro=True, skip_supervisor=True), "m1").run())

    ind = {i.id: i for i in record.indicators}
    assert ind["ecb_deposit"].value == 2.39 and ind["ecb_deposit"].source == "FRED (ECBDFR)"
    assert ind["us_cpi_yoy"].value is not None and ind["cape_sp500"].value == 36.0
    val = record.snapshot_for("sp500").valuation
    assert val.cape == 36.0 and val.percentiles["cape"] == 100.0
    assert record.snapshot_for("msci_world").valuation.cape is None  # sin cape_reference no se asigna
    assert any("percentil" in c.description for c in record.contradictions)  # 'cheap' con CAPE en máximos


def test_macro_data_can_be_disabled(monkeypatch, tmp_path, minimal_config_dict):
    from src.config import parse_config

    minimal_config_dict["data"] = {"macro_data": False}
    monkeypatch.setattr(pipeline_mod, "YFinanceProvider", FakePrices)
    settings = Settings(openai_api_key=None, tavily_api_key=None, openai_model=None, project_root=tmp_path)
    record = asyncio.run(Pipeline(parse_config(minimal_config_dict), settings, RunOptions(mode="quant"), "m2").run())
    assert not any(i.source.startswith("FRED") or i.id == "cape_sp500" for i in record.indicators)
