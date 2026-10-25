"""Tests for config validation and merge-on-save."""

from unittest import mock

import pytest
import yaml

from src.common import config


@pytest.fixture
def cfg_dir(tmp_path):
    (tmp_path / "risk.yaml").write_text(yaml.safe_dump({
        "per_trade": {"max_position_pct_of_bankroll": 0.1, "min_edge_cents": 3},
        "concentration": {"max_pct_per_event": 0.25},
        "execution": {"entry_style": "passive", "max_reprices": 6},
    }), encoding="utf-8")
    with mock.patch.object(config, "CONFIG_DIR", tmp_path):
        yield tmp_path


def test_percent_written_as_whole_number_is_rejected():
    errors = config.validate_risk({"concentration": {"max_pct_per_event": 50}})
    assert errors and "fraction" in errors[0]


def test_bad_entry_style_is_rejected():
    assert config.validate_risk({"execution": {"entry_style": "yolo"}})


def test_update_merges_and_keeps_other_keys(cfg_dir):
    merged = config.update_section("risk", {"per_trade": {"min_edge_cents": 5}})
    assert merged["per_trade"]["min_edge_cents"] == 5
    assert merged["per_trade"]["max_position_pct_of_bankroll"] == 0.1
    assert merged["execution"]["max_reprices"] == 6


def test_invalid_update_writes_nothing(cfg_dir):
    before = (cfg_dir / "risk.yaml").read_text(encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.update_section("risk", {"concentration": {"max_pct_per_event": 50}})
    assert (cfg_dir / "risk.yaml").read_text(encoding="utf-8") == before


def test_unknown_section_rejected(cfg_dir):
    with pytest.raises(config.ConfigError):
        config.update_section("secrets", {"x": 1})


def test_runtime_defaults_fill_missing(cfg_dir):
    (cfg_dir / "runtime.yaml").write_text("research:\n  interval_minutes: 60\n", encoding="utf-8")
    rt = config.load_runtime_config()
    assert rt["research"]["interval_minutes"] == 60
    assert rt["models"]["research"] == "claude-sonnet-5-5"
    assert rt["research"]["show_market_prices"] is False
