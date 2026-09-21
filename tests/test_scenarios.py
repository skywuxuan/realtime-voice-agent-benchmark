import copy
import hashlib

import pytest
import yaml
from pydantic import ValidationError

from events.replay import RecordingError
from scenarios.loader import ScenarioError, load_scenario, load_suite, load_yaml
from scenarios.schema import Scenario


def save(tmp_path, data, name="case.yaml", **kwargs):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, allow_unicode=True, **kwargs), encoding="utf-8")
    return path


def test_scenario_assets_and_canonical_hash(tmp_path, scenario_data):
    first = load_scenario(save(tmp_path, scenario_data, sort_keys=True), asset_root=tmp_path)
    second = load_scenario(
        save(tmp_path, scenario_data, "other.yaml", sort_keys=False), asset_root=tmp_path
    )
    assert first.sha256 == second.sha256
    assert first.world.now.isoformat() == "2026-09-19T10:00:00+08:00"
    options = first.model_session_options().model_dump_json()
    assert "secret oracle text" not in options and "secret city" not in options
    assert "oracle" not in options and "speech_bounds" not in options


@pytest.mark.parametrize(
    "change",
    [
        "version",
        "duplicate_action",
        "missing_asset",
        "timezone",
        "exposed_oracle",
        "negative_timeout",
    ],
)
def test_invalid_scenario_rejected(scenario_data, change):
    data = copy.deepcopy(scenario_data)
    if change == "version":
        data["schema_version"] = "0.2"
    elif change == "duplicate_action":
        data["actions"].append(data["actions"][0])
    elif change == "missing_asset":
        data["actions"][0]["asset"] = "missing"
    elif change == "timezone":
        data["world"]["now"] = "2026-09-19T10:00:00Z"
    elif change == "exposed_oracle":
        data["oracle"]["hidden_from_model"] = False
    elif change == "negative_timeout":
        data["termination"]["response_timeout_ms"] = -1
    with pytest.raises(ValidationError):
        Scenario.model_validate(data)


def test_duplicate_yaml_keys_rejected(tmp_path):
    path = tmp_path / "duplicate.yaml"
    path.write_text("schema_version: '0.1'\nschema_version: '0.2'\n")
    with pytest.raises(ScenarioError, match="duplicate YAML"):
        load_yaml(path)


@pytest.mark.parametrize("change", ["hash", "bounds", "sample_rate", "truncated"])
def test_audio_mismatch_cannot_silently_change_experiment(tmp_path, scenario_data, change):
    asset = scenario_data["audio"]["assets"]["question"]
    if change == "hash":
        asset["sha256"] = "0" * 64
    elif change == "bounds":
        asset["speech_bounds_samples"][1] = 1601
    elif change == "sample_rate":
        asset["sample_rate_hz"] = 24000
    else:
        path = tmp_path / "input.wav"
        path.write_bytes(path.read_bytes()[:-10])
        asset["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ScenarioError):
        load_scenario(save(tmp_path, scenario_data), asset_root=tmp_path)


def test_suite_rejects_duplicate_ids_even_at_different_paths(tmp_path, scenario_data):
    save(tmp_path, scenario_data)
    save(tmp_path, scenario_data, "copy.yaml")
    suite = {
        "schema_version": "0.1",
        "suite_id": "basic",
        "cases": ["case.yaml", "copy.yaml"],
        "repetitions": 3,
    }
    with pytest.raises(ScenarioError, match="scenario_id"):
        load_suite(save(tmp_path, suite, "suite.yaml"), asset_root=tmp_path)


def test_asset_symlink_cannot_escape_declared_root(tmp_path, scenario_data):
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    (asset_root / "input.wav").symlink_to(tmp_path / "input.wav")
    with pytest.raises(RecordingError, match="escapes"):
        load_scenario(save(tmp_path, scenario_data), asset_root=asset_root)


def test_response_stimulus_requires_explicit_binding(scenario_data):
    scenario_data["category"] = "backchannel"
    scenario_data["oracle"]["stimulus"] = "backchannel"
    action = scenario_data["actions"][0]
    action["stimulus"] = "backchannel"
    with pytest.raises(ValidationError, match="bound target"):
        Scenario.model_validate(scenario_data)
    action["trigger"] = {
        "type": "after_event",
        "event": "assistant_playback_start",
        "where": {"turn_id": "t0"},
        "occurrence": 1,
        "bind": {"target_response_id": "response_id"},
        "delay_ms": 800,
        "timeout_ms": 15000,
    }
    assert Scenario.model_validate(scenario_data).actions[0].stimulus == "backchannel"
