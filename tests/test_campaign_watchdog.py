import json

from scripts.campaign_watchdog import latest_activity


def test_latest_activity_tracks_progress_and_run_seals(tmp_path):
    progress = tmp_path / "progress.json"
    progress.write_text(json.dumps({"status": "running"}), encoding="utf-8")
    run_root = tmp_path / "runs"
    run = run_root / "provider-cockpit-full-window-main-001"
    run.mkdir(parents=True)
    manifest = run / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    progress.touch()
    first = latest_activity(
        progress=progress, run_root=run_root, artifact_prefix="provider"
    )
    manifest.touch()
    second = latest_activity(
        progress=progress, run_root=run_root, artifact_prefix="provider"
    )
    assert second >= first


def test_latest_activity_ignores_other_campaigns(tmp_path):
    run_root = tmp_path / "runs"
    other = run_root / "other-cockpit-full-window-main-001"
    other.mkdir(parents=True)
    (other / "manifest.json").write_text("{}", encoding="utf-8")
    assert latest_activity(
        progress=tmp_path / "missing.json",
        run_root=run_root,
        artifact_prefix="provider",
    ) == 0
