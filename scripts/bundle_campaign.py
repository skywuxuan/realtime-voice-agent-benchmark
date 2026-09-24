"""Merge compact Agent runs into one campaign-level evidence bundle."""

from __future__ import annotations

import argparse
import copy
import json
import shutil
from pathlib import Path

from agent.artifacts import (
    BUNDLE_ARCHIVE_NAME,
    BUNDLE_FORMAT_VERSION,
    BUNDLE_MANIFEST,
    RESULTS_NAME,
    _bundle_archive_records,
    _bundle_file_records,
    _tree_digest,
    _write_bundle_archive,
    latency_summary,
    load_compact_results,
    verify_bundle,
    verify_compact_run,
)
from benchmark.contracts import pretty_json
from events.replay import file_hash


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(pretty_json(value), encoding="utf-8")
    temporary.replace(path)


def bundle_campaign(
    *, bundle_root: Path, runs: tuple[Path, ...], remove_sources: bool = True
) -> dict:
    if not runs:
        raise ValueError("at least one compact run is required")
    runs = tuple(sorted(path.resolve() for path in runs))
    if len(set(runs)) != len(runs):
        raise ValueError("bundle inputs must be unique")
    if bundle_root.exists():
        raise ValueError(f"bundle output already exists: {bundle_root}")
    if any(path == bundle_root or bundle_root.is_relative_to(path) for path in runs):
        raise ValueError("bundle output cannot be inside an input run")

    source_runs = []
    archive_records = []
    result_rows = []
    profiles = set()
    for root in runs:
        verify_compact_run(root, deep=True)
        compact = json.loads((root / "compact.json").read_text(encoding="utf-8"))
        results = load_compact_results(root)
        run_name = root.name
        profiles.add(json.dumps(compact.get("timing_profile"), sort_keys=True))
        source_runs.append(
            {
                "name": run_name,
                "path": root.relative_to(root.parent.parent).as_posix()
                if root.parent.name != "bundles"
                else str(root),
                "run_id": compact["run_id"],
                "compact_sha256": file_hash(root / "compact.json"),
                "attempt_count": compact["attempt_count"],
            }
        )
        archive_records.extend(_bundle_file_records(root, run_name))
        for row in results["cases"]:
            item = copy.deepcopy(row)
            item["source_run"] = run_name
            item["artifact_path"] = f"{run_name}/{row['artifact_path']}"
            item["archive_member_prefix"] = item["artifact_path"]
            result_rows.append(item)

    result_rows.sort(key=lambda row: (row.get("source_line_number") or 0, row["artifact_path"]))
    result = {
        "schema_version": "0.1",
        "kind": "compact_agent_bundle_results",
        "bundle_id": bundle_root.name,
        "timing_definition": {
            "start": "user_audio_end",
            "function_call_end": "last tool_call_end",
            "tts_first_frame": "first assistant_audio_start",
            "unit": "milliseconds",
        },
        "latency_summary": latency_summary(result_rows),
        "cases": result_rows,
    }

    bundle_root.mkdir(parents=True)
    results_path = bundle_root / RESULTS_NAME
    _write_json(results_path, result)
    temporary_archive = bundle_root / (BUNDLE_ARCHIVE_NAME + ".tmp")
    _write_bundle_archive(archive_records, temporary_archive)
    archived_records = _bundle_archive_records(temporary_archive)
    expected_records = [
        {key: record[key] for key in ("path", "sha256", "bytes")} for record in archive_records
    ]
    if len(archived_records) != len(expected_records) or _tree_digest(archived_records) != _tree_digest(
        expected_records
    ):
        shutil.rmtree(bundle_root)
        raise ValueError("bundle archive failed content verification")
    archive_path = bundle_root / BUNDLE_ARCHIVE_NAME
    temporary_archive.replace(archive_path)

    manifest = {
        "schema_version": "0.1",
        "kind": "compact_agent_bundle",
        "format": BUNDLE_FORMAT_VERSION,
        "status": "complete",
        "bundle_id": bundle_root.name,
        "source_runs": source_runs,
        "results": {
            "path": RESULTS_NAME,
            "sha256": file_hash(results_path),
            "bytes": results_path.stat().st_size,
        },
        "archive": {
            "path": BUNDLE_ARCHIVE_NAME,
            "sha256": file_hash(archive_path),
            "bytes": archive_path.stat().st_size,
            "member_count": len(archive_records),
            "tree_sha256": _tree_digest(expected_records),
        },
        "attempt_count": len(result_rows),
        "timing_profiles": [json.loads(profile) for profile in sorted(profiles)],
    }
    _write_json(bundle_root / BUNDLE_MANIFEST, manifest)
    verify_bundle(bundle_root, deep=True)
    if remove_sources:
        for root in runs:
            shutil.rmtree(root)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--run", type=Path, action="append", required=True)
    parser.add_argument("--keep-sources", action="store_true")
    args = parser.parse_args()
    result = bundle_campaign(
        bundle_root=args.bundle_root,
        runs=tuple(args.run),
        remove_sources=not args.keep_sources,
    )
    print(
        json.dumps(
            {
                "bundle": str(args.bundle_root),
                "source_runs": len(result["source_runs"]),
                "attempts": result["attempt_count"],
                "archive_bytes": result["archive"]["bytes"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
