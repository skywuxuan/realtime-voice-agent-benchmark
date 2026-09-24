"""Monitor a user-systemd benchmark campaign and restart stalled work."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from benchmark.contracts import pretty_json


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _progress(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def latest_activity(*, progress: Path, run_root: Path, artifact_prefix: str) -> float:
    observed = [progress.stat().st_mtime] if progress.exists() else []
    for root in run_root.glob(f"{artifact_prefix}-cockpit-full-*"):
        for name in ("manifest.json", "metrics.json", "compact.json", "results.json"):
            path = root / name
            if path.exists():
                observed.append(path.stat().st_mtime)
    return max(observed, default=0.0)


def unit_state(unit: str) -> str:
    result = subprocess.run(
        ["systemctl", "--user", "show", unit, "--property=ActiveState", "--value"],
        capture_output=True,
        check=False,
        text=True,
    )
    return result.stdout.strip() or "not-found"


def restart_unit(unit: str) -> None:
    result = subprocess.run(
        ["systemctl", "--user", "restart", unit],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(f"systemd restart failed with status {result.returncode}")


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(pretty_json(state), encoding="utf-8")
    temporary.replace(path)


def monitor(
    *,
    unit: str,
    progress_path: Path,
    run_root: Path,
    artifact_prefix: str,
    state_path: Path,
    poll_seconds: float,
    stall_seconds: float,
    max_restarts: int,
) -> None:
    started = time.time()
    restarts = 0
    last_restart = None
    while True:
        now = time.time()
        progress = _progress(progress_path)
        activity = latest_activity(
            progress=progress_path, run_root=run_root, artifact_prefix=artifact_prefix
        )
        active_state = unit_state(unit)
        shards = progress.get("shards", [])
        state = {
            "schema_version": "0.1",
            "kind": "campaign_watchdog",
            "updated_at": _timestamp(),
            "unit": unit,
            "unit_state": active_state,
            "campaign_status": progress.get("status", "not_started"),
            "sealed_shards": len(shards),
            "sealed_end_line": max((row["end_line"] for row in shards), default=0),
            "latest_activity_at": datetime.fromtimestamp(activity, UTC).isoformat()
            if activity
            else None,
            "idle_seconds": round(now - activity, 3) if activity else round(now - started, 3),
            "restart_count": restarts,
            "last_restart_at": last_restart,
        }
        if progress.get("status") == "complete":
            state["status"] = "complete"
            _write_state(state_path, state)
            return

        idle = now - activity if activity else now - started
        reason = None
        if active_state not in {"active", "activating"}:
            reason = f"unit_{active_state}"
        elif idle >= stall_seconds:
            reason = "activity_timeout"
        if reason:
            if restarts >= max_restarts:
                state.update(status="failed", reason="restart_limit_reached")
                _write_state(state_path, state)
                raise RuntimeError("campaign watchdog restart limit reached")
            restart_unit(unit)
            restarts += 1
            last_restart = _timestamp()
            state.update(
                status="restarted",
                reason=reason,
                restart_count=restarts,
                last_restart_at=last_restart,
            )
        else:
            state["status"] = "monitoring"
        _write_state(state_path, state)
        time.sleep(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit", required=True)
    parser.add_argument("--progress", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, default=Path("runs"))
    parser.add_argument("--artifact-prefix", required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=float, default=60)
    parser.add_argument("--stall-seconds", type=float, default=900)
    parser.add_argument("--max-restarts", type=int, default=8)
    args = parser.parse_args()
    if args.poll_seconds <= 0 or args.stall_seconds <= args.poll_seconds or args.max_restarts < 1:
        parser.error("invalid watchdog polling, stall timeout, or restart limit")
    monitor(
        unit=args.unit,
        progress_path=args.progress,
        run_root=args.run_root,
        artifact_prefix=args.artifact_prefix,
        state_path=args.state,
        poll_seconds=args.poll_seconds,
        stall_seconds=args.stall_seconds,
        max_restarts=args.max_restarts,
    )


if __name__ == "__main__":
    main()
