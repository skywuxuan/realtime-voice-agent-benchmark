import json
from pathlib import Path

from benchmark.provider_probe import probe


def test_deferred_provider_probe_is_explicit(tmp_path: Path):
    result = probe("step-realtime", tmp_path / "probe")
    assert result["status"] == "deferred"
    saved = json.loads((tmp_path / "probe/probe.json").read_text())
    assert saved["capabilities"]["status"] == "unknown"
