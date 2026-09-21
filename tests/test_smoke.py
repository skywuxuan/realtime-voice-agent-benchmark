import asyncio

from benchmark.smoke import run_smoke
from events.replay import read_recording
from scenarios.loader import load_scenario


def test_end_to_end_offline_contract_demo(tmp_path):
    result = asyncio.run(run_smoke(tmp_path / "smoke"))
    assert result["audio_replay_verified"] is True
    assert result["mode"] == "synthetic_fixture_not_model_benchmark"
    scenario = load_scenario(tmp_path / "smoke" / "scenario.yaml", asset_root=tmp_path / "smoke")
    recording = read_recording(tmp_path / "smoke" / "recording")
    assert recording.manifest["status"] == "complete"
    assert recording.events[0].scenario_id == scenario.scenario_id
    assert {row.direction for row in recording.raw_events} == {"sent", "received"}
    assert not (tmp_path / "smoke" / "metrics.json").exists()
