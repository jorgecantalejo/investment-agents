"""Tests de validación de la configuración YAML."""

from pathlib import Path

import pytest

from src.config import load_config, parse_config
from src.core.errors import ConfigError

ROOT = Path(__file__).resolve().parent.parent


def test_project_configs_are_valid():
    for path in (ROOT / "config.yaml", ROOT / "config.example.yaml"):
        if not path.exists():  # config.yaml es personal y no se sube al repositorio
            continue
        cfg = load_config(path)
        assert {a.id for a in cfg.assets} >= {"msci_world", "sp500", "nasdaq", "gold", "bitcoin", "commodities", "energy"}
        assert cfg.rules.tactical_budget_eur > 0


def test_minimal_config_gets_defaults(config):
    assert config.rules.horizon_years == 10
    assert config.benchmark_for(config.assets[1]) == "IWDA.AS"


def test_duplicate_ids_rejected(minimal_config_dict):
    minimal_config_dict["assets"].append(dict(minimal_config_dict["assets"][0]))
    with pytest.raises(ConfigError, match="duplicados"):
        parse_config(minimal_config_dict)


def test_single_buy_above_budget_rejected(minimal_config_dict):
    minimal_config_dict["rules"] = {"tactical_budget_eur": 100, "max_single_buy_eur": 200}
    with pytest.raises(ConfigError, match="max_single_buy_eur"):
        parse_config(minimal_config_dict)


@pytest.mark.parametrize(
    "patch",
    [
        {"asset_class": "acciones"},  # clase no permitida
        {"id": "MSCI World"},          # id con espacios/mayúsculas
        {"currency": "euro"},
        {"unknown_field": 1},          # campos desconocidos -> error (evita erratas silenciosas)
    ],
)
def test_invalid_asset_fields_rejected(minimal_config_dict, patch):
    minimal_config_dict["assets"][1].update(patch)
    with pytest.raises(ConfigError):
        parse_config(minimal_config_dict)


def test_select_assets(config):
    assert [a.id for a in config.select_assets(["gold"])] == ["gold"]
    with pytest.raises(ValueError, match="desconocidos"):
        config.select_assets(["bitcoin"])


def test_missing_file_and_bad_yaml(tmp_path):
    with pytest.raises(ConfigError, match="No existe"):
        load_config(tmp_path / "nope.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("assets: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML"):
        load_config(bad)


def test_profiles_resolve_in_config_order(minimal_config_dict):
    minimal_config_dict["profiles"] = {"todo": {"assets": "all"}, "oro": {"assets": ["gold"], "title": "Oro"}}
    cfg = parse_config(minimal_config_dict)
    assert cfg.resolve_profiles(["oro", "todo"]) == {"oro": ["gold"], "todo": ["msci_world", "gold"]}
    with pytest.raises(ValueError, match="perfiles desconocidos"):
        cfg.resolve_profiles(["nada"])


@pytest.mark.parametrize("profiles, error", [
    ({"x": {"assets": ["bitcoin"]}}, "activos desconocidos"),
    ({"x": {"assets": []}}, "no tiene activos"),
    ({"Mal-Nombre": {"assets": "all"}}, "nombre de perfil"),
])
def test_invalid_profiles_rejected(minimal_config_dict, profiles, error):
    minimal_config_dict["profiles"] = profiles
    with pytest.raises(ConfigError, match=error):
        parse_config(minimal_config_dict)
