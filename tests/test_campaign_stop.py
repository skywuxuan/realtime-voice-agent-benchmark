import json

from scripts.stop_campaigns_at_line import sealed_end, write_state


def test_campaign_stop_state_uses_only_sealed_shards(tmp_path):
    progress = tmp_path / "progress.json"
    progress.write_text(
        json.dumps({"status": "running", "shards": [{"start_line": 1, "end_line": 100}]}),
        encoding="utf-8",
    )
    assert sealed_end(progress) == 100
    state = tmp_path / "state.json"
    write_state(state, {"target_line": 4000, "stopped": False})
    assert json.loads(state.read_text(encoding="utf-8")) == {
        "target_line": 4000,
        "stopped": False,
    }
