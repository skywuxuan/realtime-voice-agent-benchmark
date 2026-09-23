import json
from argparse import Namespace
from pathlib import Path

import pytest

from dataset.compiler import MissingRenderedAudio
from dataset.schema import TTSProfile
from events.replay import file_hash
from scenarios.loader import load_yaml
from scripts.cockpit_campaign import pending_windows, run_window, source_windows


def test_source_windows_cover_exact_range_without_overlap():
    assert list(source_windows(21, 235, 100)) == [(21, 120), (121, 220), (221, 235)]


def test_seed_campaign_names_do_not_collide_with_qwen():
    seed = Namespace(
        dataset_prefix="cockpit_seed_duplex3",
        artifact_prefix="seed-duplex3",
        report_prefix="cockpit-seed-duplex3",
    )
    assert seed.dataset_prefix != "cockpit_audio3"
    assert seed.artifact_prefix != "qwen-audio3"
    assert seed.report_prefix != "cockpit-audio3"


def test_cache_only_campaign_waits_for_qwen_audio_without_invoking_tts(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    profile = TTSProfile.model_validate(
        load_yaml(Path(__file__).resolve().parents[1] / "configs/tts/qwen-cherry.yaml")
    )
    calls, sleeps = [], []

    def compile_fixture(**kwargs):
        calls.append((kwargs["allow_render"], kwargs["secrets"]))
        if len(calls) == 1:
            raise MissingRenderedAudio("Qwen audio is not frozen yet")
        raise RuntimeError("stop after verifying cache-only retry")

    monkeypatch.setattr("scripts.cockpit_campaign.compile_cockpit_dataset", compile_fixture)
    monkeypatch.setattr("scripts.cockpit_campaign.time.sleep", sleeps.append)
    args = Namespace(
        dataset_prefix="cockpit_seed_duplex3_cacheonly",
        protocol=Path("unused_protocol.jsonl"),
        testset=Path("unused_testset.jsonl"),
        asset_root=Path("."),
        target_model="seed-duplex-3.0",
        input_chunk_ms=20,
        cache_only=True,
        await_cache=True,
        cache_wait_s=30,
        tts_attempts=6,
    )
    with pytest.raises(RuntimeError, match="stop after verifying"):
        run_window(args, 1, 100, profile)
    assert calls == [(False, ()), (False, ())]
    assert sleeps == [30]


def test_campaign_reuses_sealed_prefix_without_regenerating_old_report(tmp_path):
    conversion = tmp_path / "conversion.json"
    conversion.write_text("{}", encoding="utf-8")
    compilation = tmp_path / "compilation.json"
    compilation.write_text("{}", encoding="utf-8")
    run = tmp_path / "run"
    run.mkdir()
    manifest = {"kind": "agent_run", "status": "complete", "run_id": "run_1", "attempts": []}
    metrics = {"evaluation_id": "eval_1", "agent": {"counts": {}, "cases": []}}
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (run / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    evaluation = run / "evaluations" / "eval_1"
    evaluation.mkdir(parents=True)
    (evaluation / "config.json").write_text(
        json.dumps({"manifest_sha256": file_hash(run / "manifest.json")}), encoding="utf-8"
    )
    (evaluation / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    report_path = tmp_path / "report.json"
    report = {
        "source_conversion": {"manifest_sha256": file_hash(conversion)},
        "compilation": {"manifest_sha256": file_hash(compilation)},
        "counts": {"eligible": 0},
        "runs": [
            {
                "path": str(run),
                "manifest_sha256": file_hash(run / "manifest.json"),
                "run_id": "run_1",
                "evaluation_id": "eval_1",
                "counts": {},
            }
        ],
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    shard = {
        "start_line": 1,
        "end_line": 100,
        "valid_tool": 100,
        "invalid_tool": 0,
        "no_tool": 0,
        "compilation_manifest": str(compilation),
        "report": str(report_path),
        "runs": [str(run)],
        "counts": {"eligible": 0},
    }
    assert pending_windows(
        windows=source_windows(1, 200, 100),
        sealed_shards=[shard],
        conversion_manifest=conversion,
    ) == ((101, 200),)
    assert json.loads(report_path.read_text(encoding="utf-8")) == report
    with pytest.raises(ValueError, match="contiguous"):
        pending_windows(
            windows=source_windows(1, 200, 100),
            sealed_shards=[{**shard, "start_line": 2}],
            conversion_manifest=conversion,
        )
    report["counts"]["eligible"] = 1
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="sealed shard report"):
        pending_windows(
            windows=source_windows(1, 200, 100),
            sealed_shards=[shard],
            conversion_manifest=conversion,
        )
