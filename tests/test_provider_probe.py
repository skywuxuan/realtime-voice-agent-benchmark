import json
from pathlib import Path

from benchmark.provider_probe import probe


def test_step_provider_probe_exposes_live_credential_boundary(tmp_path: Path):
    result = probe("step-realtime", tmp_path / "probe")
    assert result["status"] == "live_supported"
    assert result["credential_variables"] == ("STEPFUN_API_KEY",)
    saved = json.loads((tmp_path / "probe/probe.json").read_text())
    assert saved["capabilities"]["status"] == "unknown"
