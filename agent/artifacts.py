"""Compact sealed Agent runs without losing their original evidence."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import shutil
import statistics
import tarfile
import uuid
from functools import lru_cache
from pathlib import Path, PurePosixPath

from benchmark.contracts import canonical_json, pretty_json
from events.replay import file_hash

ARCHIVE_NAME = "evidence.tar.gz"
BUNDLE_ARCHIVE_NAME = "evidence.tar"
COMPACT_MANIFEST = "compact.json"
BUNDLE_MANIFEST = "bundle.json"
EXAMPLE_SESSION_CONFIG = "example-session-config.json"
RESULTS_NAME = "results.json"
FORMAT_VERSION = "agent-compact-0.1"
BUNDLE_FORMAT_VERSION = "agent-bundle-0.1"


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json_atomic(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(pretty_json(value), encoding="utf-8")
    temporary.replace(path)


def _file_digest(stream) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _tree_digest(records: list[dict]) -> str:
    digest = hashlib.sha256()
    for record in sorted(records, key=lambda item: item["path"]):
        digest.update(
            (canonical_json({key: record[key] for key in ("path", "sha256", "bytes")}) + "\n").encode(
                "utf-8"
            )
        )
    return digest.hexdigest()


def _source_records(root: Path) -> list[dict]:
    cases = root / "cases"
    if not cases.is_dir():
        raise ValueError(f"loose case artifacts are unavailable: {root}")
    records = []
    for path in sorted(item for item in cases.rglob("*") if item.is_file()):
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": file_hash(path),
                "bytes": path.stat().st_size,
                "source": path,
            }
        )
    return records


def _validate_run(root: Path, records: list[dict]) -> tuple[dict, dict]:
    manifest_path = root / "manifest.json"
    manifest = _read_json(manifest_path)
    if manifest.get("kind") != "agent_run" or manifest.get("status") != "complete":
        raise ValueError(f"only completed Agent runs can be compacted: {root}")
    metrics = _read_json(root / "metrics.json")
    evaluation = root / "evaluations" / metrics["evaluation_id"]
    evaluation_config = _read_json(evaluation / "config.json")
    if evaluation_config["manifest_sha256"] != file_hash(manifest_path):
        raise ValueError(f"run changed since evaluation: {root}")
    if _read_json(evaluation / "metrics.json") != metrics:
        raise ValueError(f"run metrics differ from sealed evaluation: {root}")

    by_path = {record["path"]: record for record in records}
    metric_cases = metrics["agent"]["cases"]
    if len(manifest["attempts"]) != len(metric_cases):
        raise ValueError(f"run index differs from evaluation: {root}")
    for entry, case in zip(manifest["attempts"], metric_cases):
        if (
            entry["scenario_id"] != case["scenario_id"]
            or entry["attempt_id"] != case["attempt_id"]
            or case.get("artifact_path") != entry["path"]
        ):
            raise ValueError(f"case index differs from evaluation: {root}")
        case_manifest_name = f'{entry["path"]}/manifest.json'
        case_manifest_record = by_path.get(case_manifest_name)
        if (
            case_manifest_record is None
            or case_manifest_record["sha256"] != entry["manifest_sha256"]
        ):
            raise ValueError(f"case manifest differs from run index: {case_manifest_name}")
        sealed = _read_json(root / case_manifest_name)
        for name, info in sealed.get("files", {}).items():
            record = by_path.get(f'{entry["path"]}/{name}')
            if record is None or (record["sha256"], record["bytes"]) != (
                info["sha256"],
                info["bytes"],
            ):
                raise ValueError(f"sealed case artifact differs: {entry['path']}/{name}")
    return manifest, metrics


def case_timing(events_path: Path) -> dict:
    speech_end = None
    calls = []
    audio_starts = []
    if events_path.exists():
        with events_path.open(encoding="utf-8") as events:
            for line in events:
                event = json.loads(line)
                if event["event"] == "user_audio_end":
                    speech_end = event
                elif event["event"] == "tool_call_end":
                    calls.append(event)
                elif event["event"] == "assistant_audio_start":
                    audio_starts.append(event)
    if speech_end is None:
        relevant_calls = []
        relevant_audio = []
        start_ns = None
    else:
        start_ns = speech_end["timestamp_monotonic_ns"]
        clock_id = speech_end["clock_id"]
        relevant_calls = [
            event
            for event in calls
            if event["clock_id"] == clock_id and event["timestamp_monotonic_ns"] >= start_ns
        ]
        relevant_audio = [
            event
            for event in audio_starts
            if event["clock_id"] == clock_id and event["timestamp_monotonic_ns"] >= start_ns
        ]
    final_call_ns = (
        max(event["timestamp_monotonic_ns"] for event in relevant_calls)
        if relevant_calls
        else None
    )
    first_audio_ns = (
        min(event["timestamp_monotonic_ns"] for event in relevant_audio)
        if relevant_audio
        else None
    )
    return {
        "speech_end_monotonic_ns": start_ns,
        "final_tool_call_monotonic_ns": final_call_ns,
        "first_tts_frame_monotonic_ns": first_audio_ns,
        "speech_end_to_final_tool_call_ms": (
            (final_call_ns - start_ns) / 1_000_000
            if final_call_ns is not None and start_ns is not None
            else None
        ),
        "speech_end_to_first_tts_frame_ms": (
            (first_audio_ns - start_ns) / 1_000_000
            if first_audio_ns is not None and start_ns is not None
            else None
        ),
        "tool_call_count": len(relevant_calls),
    }


def latency_summary(rows: list[dict]) -> dict:
    def percentile(values: list[float], quantile: float) -> float:
        ordered = sorted(values)
        index = (len(ordered) - 1) * quantile
        lower = math.floor(index)
        upper = math.ceil(index)
        if lower == upper:
            return ordered[lower]
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)

    def describe(field: str) -> dict:
        values = [row["timing"][field] for row in rows if row["timing"][field] is not None]
        def rounded(value: float | None) -> float | None:
            return round(value, 3) if value is not None else None

        return {
            "n": len(values),
            "missing": len(rows) - len(values),
            "mean_ms": rounded(statistics.fmean(values)) if values else None,
            "p50_ms": rounded(percentile(values, 0.50)) if values else None,
            "p90_ms": rounded(percentile(values, 0.90)) if values else None,
            "p95_ms": rounded(percentile(values, 0.95)) if values else None,
            "p99_ms": rounded(percentile(values, 0.99)) if values else None,
            "min_ms": rounded(min(values)) if values else None,
            "max_ms": rounded(max(values)) if values else None,
        }

    return {
        "speech_end_to_final_tool_call": describe("speech_end_to_final_tool_call_ms"),
        "speech_end_to_first_tts_frame": describe("speech_end_to_first_tts_frame_ms"),
    }


def _result_payload(root: Path, manifest: dict, metrics: dict) -> tuple[dict, dict, dict]:
    rows = []
    timing_profile = None
    example_session = None
    for entry, evaluation in zip(manifest["attempts"], metrics["agent"]["cases"]):
        case_root = root / entry["path"]
        scenario = _read_json(case_root / "scenario.json")
        transcript = _read_json(case_root / "transcript.json")
        tool_calls = _read_json(case_root / "tool_calls.json")
        config = _read_json(case_root / "config.json")
        profile = config.get("profile")
        if timing_profile is not None and profile != timing_profile:
            raise ValueError(f"run contains mixed timing profiles: {root}")
        timing_profile = profile
        session = _read_json(case_root / "session_config.json")
        if example_session is None:
            example_session = session
        rows.append(
            {
                "artifact_path": entry["path"],
                "archive_member_prefix": entry["path"],
                "attempt_id": entry["attempt_id"],
                "attempt_manifest_sha256": entry["manifest_sha256"],
                "scenario_id": entry["scenario_id"],
                "source_line_number": scenario.get("world", {}).get("source_line_number"),
                "user_turns": scenario.get("user_turns", []),
                "expected_calls": scenario.get("expected_calls", []),
                "input_audio_assets": scenario.get("audio_assets", {}),
                "evaluation": evaluation,
                "tool_calls": tool_calls,
                "transcript": transcript,
                "timing": case_timing(case_root / "events.jsonl"),
            }
        )
    result = {
        "schema_version": "0.1",
        "kind": "compact_agent_run_results",
        "run_id": manifest["run_id"],
        "evaluation_id": metrics["evaluation_id"],
        "timing_definition": {
            "start": "user_audio_end",
            "function_call_end": "last tool_call_end",
            "tts_first_frame": "first assistant_audio_start",
            "unit": "milliseconds",
        },
        "latency_summary": latency_summary(rows),
        "cases": rows,
    }
    return result, timing_profile, example_session or {"status": "unavailable"}


def _write_archive(root: Path, records: list[dict], destination: Path) -> None:
    seen: dict[tuple[str, int], str] = {}
    with destination.open("xb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=1, mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for record in records:
                    info = tarfile.TarInfo(record["path"])
                    info.mode = 0o644
                    info.mtime = 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    key = (record["sha256"], record["bytes"])
                    if key in seen:
                        info.type = tarfile.LNKTYPE
                        info.linkname = seen[key]
                        info.size = 0
                        archive.addfile(info)
                        continue
                    seen[key] = record["path"]
                    info.size = record["bytes"]
                    with record["source"].open("rb") as stream:
                        archive.addfile(info, stream)


def _archive_records(path: Path) -> list[dict]:
    records = []
    seen: dict[str, tuple[str, int]] = {}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            name = member.name
            pure = PurePosixPath(name)
            if (
                not name
                or pure.is_absolute()
                or ".." in pure.parts
                or not pure.parts
                or pure.parts[0] != "cases"
                or str(pure) != name
            ):
                raise ValueError(f"unsafe compact archive member: {name}")
            if member.isreg():
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError(f"unreadable compact archive member: {name}")
                digest, size = _file_digest(stream)
                if size != member.size:
                    raise ValueError(f"compact archive member size mismatch: {name}")
            elif member.islnk():
                if member.linkname not in seen:
                    raise ValueError(f"compact archive hard link has no prior target: {name}")
                digest, size = seen[member.linkname]
            else:
                raise ValueError(f"unsupported compact archive member: {name}")
            seen[name] = (digest, size)
            records.append({"path": name, "sha256": digest, "bytes": size})
    return records


def _load_bundle_results(root: Path) -> dict:
    bundle = verify_bundle(root)
    results = _read_json(root / bundle["results"]["path"])
    if results.get("kind") != "compact_agent_bundle_results":
        raise ValueError(f"unsupported bundle results: {root}")
    return results


@lru_cache(maxsize=None)
def _load_compact_results(root_value: str) -> dict:
    root = Path(root_value)
    if (root / BUNDLE_MANIFEST).exists():
        return _load_bundle_results(root)
    compact = verify_compact_run(root)
    results = _read_json(root / compact["results"]["path"])
    if results.get("kind") != "compact_agent_run_results":
        raise ValueError(f"unsupported compact results: {root}")
    return results


def load_compact_results(root: str | Path) -> dict:
    return _load_compact_results(str(Path(root).resolve()))


def compact_case(root: str | Path, artifact_path: str) -> dict:
    results = load_compact_results(root)
    matches = [row for row in results["cases"] if row["artifact_path"] == artifact_path]
    if len(matches) != 1:
        raise ValueError(f"compact run does not contain one case: {artifact_path}")
    return matches[0]


@lru_cache(maxsize=None)
def resolve_bundle_reference(run_root_value: str) -> tuple[Path, str] | None:
    """Map a deleted run path and case path to its campaign bundle."""
    run_root = Path(run_root_value)
    source = run_root.as_posix()
    bundles_root = run_root.parent / "bundles"
    if not bundles_root.is_dir():
        return None
    for bundle_root in sorted(path for path in bundles_root.iterdir() if path.is_dir()):
        manifest_path = bundle_root / BUNDLE_MANIFEST
        if not manifest_path.exists():
            continue
        manifest = _read_json(manifest_path)
        for entry in manifest.get("source_runs", []):
            if entry.get("path") == source:
                return bundle_root, entry["name"]
    return None


def artifact_display_path(root: str | Path, artifact_path: str) -> str:
    root = Path(root)
    archive_name = (
        _read_json(root / BUNDLE_MANIFEST).get("archive", {}).get("path", BUNDLE_ARCHIVE_NAME)
        if (root / BUNDLE_MANIFEST).exists()
        else ARCHIVE_NAME
    )
    return f"{root / archive_name}#{artifact_path}"


def _bundle_file_records(root: Path, run_name: str) -> list[dict]:
    records = []
    for relative in (
        "manifest.json",
        "metrics.json",
        "config.json",
        "compact.json",
        "example-session-config.json",
        "results.json",
        ARCHIVE_NAME,
    ):
        path = root / relative
        if not path.is_file():
            raise ValueError(f"compact run is missing bundle input: {path}")
        records.append(
            {
                "path": f"{run_name}/{relative}",
                "sha256": file_hash(path),
                "bytes": path.stat().st_size,
                "source": path,
            }
        )
    return records


def _write_bundle_archive(records: list[dict], destination: Path) -> None:
    with destination.open("xb") as raw:
        with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for record in records:
                info = tarfile.TarInfo(record["path"])
                info.mode = 0o644
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.size = record["bytes"]
                with record["source"].open("rb") as stream:
                    archive.addfile(info, stream)


def _bundle_archive_records(path: Path) -> list[dict]:
    records = []
    with tarfile.open(path, "r") as archive:
        for member in archive:
            pure = PurePosixPath(member.name)
            if (
                not member.name
                or pure.is_absolute()
                or ".." in pure.parts
                or len(pure.parts) < 2
                or str(pure) != member.name
            ):
                raise ValueError(f"unsafe bundle archive member: {member.name}")
            if not member.isreg():
                raise ValueError(f"unsupported bundle archive member: {member.name}")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError(f"unreadable bundle archive member: {member.name}")
            digest, size = _file_digest(stream)
            if size != member.size:
                raise ValueError(f"bundle archive member size mismatch: {member.name}")
            records.append({"path": member.name, "sha256": digest, "bytes": size})
    return records


def verify_bundle(root: str | Path, *, deep: bool = False) -> dict:
    root = Path(root)
    bundle = _read_json(root / BUNDLE_MANIFEST)
    if bundle.get("format") != BUNDLE_FORMAT_VERSION or bundle.get("status") != "complete":
        raise ValueError(f"unsupported or incomplete bundle: {root}")
    for name in ("results",):
        reference = bundle[name]
        path = root / reference["path"]
        if path.stat().st_size != reference["bytes"] or file_hash(path) != reference["sha256"]:
            raise ValueError(f"bundle reference differs: {path}")
    archive_ref = bundle["archive"]
    archive = root / archive_ref["path"]
    if archive.stat().st_size != archive_ref["bytes"] or file_hash(archive) != archive_ref["sha256"]:
        raise ValueError(f"bundle archive differs: {archive}")
    if deep:
        records = _bundle_archive_records(archive)
        if (
            len(records) != archive_ref["member_count"]
            or _tree_digest(records) != archive_ref["tree_sha256"]
        ):
            raise ValueError(f"bundle archive content differs: {archive}")
    return bundle


def verify_compact_run(root: str | Path, *, deep: bool = False) -> dict:
    root = Path(root)
    compact = _read_json(root / COMPACT_MANIFEST)
    if compact.get("format") != FORMAT_VERSION or compact.get("status") != "complete":
        raise ValueError(f"unsupported or incomplete compact run: {root}")
    for name in ("source_manifest", "source_metrics", "results", "example_session_config"):
        reference = compact[name]
        path = root / reference["path"]
        if path.stat().st_size != reference["bytes"] or file_hash(path) != reference["sha256"]:
            raise ValueError(f"compact run reference differs: {path}")
    archive_ref = compact["archive"]
    archive = root / archive_ref["path"]
    if archive.stat().st_size != archive_ref["bytes"] or file_hash(archive) != archive_ref["sha256"]:
        raise ValueError(f"compact archive differs: {archive}")
    if deep:
        records = _archive_records(archive)
        if (
            len(records) != archive_ref["member_count"]
            or sum(record["bytes"] for record in records) != archive_ref["logical_bytes"]
            or _tree_digest(records) != archive_ref["tree_sha256"]
        ):
            raise ValueError(f"compact archive content differs: {archive}")
    return compact


def compact_run(root: str | Path, *, remove_loose: bool = True) -> dict:
    root = Path(root)
    compact_path = root / COMPACT_MANIFEST
    if compact_path.exists():
        compact = verify_compact_run(root, deep=True)
        if remove_loose and (root / "cases").exists():
            shutil.rmtree(root / "cases")
        (root / "report.html").unlink(missing_ok=True)
        return compact

    records = _source_records(root)
    manifest, metrics = _validate_run(root, records)
    results, timing_profile, example_session = _result_payload(root, manifest, metrics)
    results_path = root / RESULTS_NAME
    example_path = root / EXAMPLE_SESSION_CONFIG
    _write_json_atomic(results_path, results)
    _write_json_atomic(example_path, example_session)

    archive = root / ARCHIVE_NAME
    temporary = archive.with_suffix(archive.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    if archive.exists():
        archive.unlink()
    _write_archive(root, records, temporary)
    archived_records = _archive_records(temporary)
    tree_sha256 = _tree_digest(
        [{key: record[key] for key in ("path", "sha256", "bytes")} for record in records]
    )
    if (
        len(archived_records) != len(records)
        or sum(record["bytes"] for record in archived_records)
        != sum(record["bytes"] for record in records)
        or _tree_digest(archived_records) != tree_sha256
    ):
        temporary.unlink(missing_ok=True)
        raise ValueError(f"new compact archive failed verification: {root}")
    temporary.replace(archive)

    compact = {
        "schema_version": "0.1",
        "kind": "compact_agent_run",
        "format": FORMAT_VERSION,
        "status": "complete",
        "run_id": manifest["run_id"],
        "source_manifest": {
            "path": "manifest.json",
            "sha256": file_hash(root / "manifest.json"),
            "bytes": (root / "manifest.json").stat().st_size,
        },
        "source_metrics": {
            "path": "metrics.json",
            "sha256": file_hash(root / "metrics.json"),
            "bytes": (root / "metrics.json").stat().st_size,
        },
        "results": {
            "path": RESULTS_NAME,
            "sha256": file_hash(results_path),
            "bytes": results_path.stat().st_size,
        },
        "example_session_config": {
            "path": EXAMPLE_SESSION_CONFIG,
            "sha256": file_hash(example_path),
            "bytes": example_path.stat().st_size,
        },
        "archive": {
            "path": ARCHIVE_NAME,
            "compression": "gzip-1",
            "sha256": file_hash(archive),
            "bytes": archive.stat().st_size,
            "member_count": len(records),
            "logical_bytes": sum(record["bytes"] for record in records),
            "tree_sha256": tree_sha256,
        },
        "attempt_count": len(manifest["attempts"]),
        "timing_profile": timing_profile,
        "input_audio_policy": "reference frozen content-addressed assets; do not prune cache",
    }
    _write_json_atomic(compact_path, compact)
    if remove_loose:
        shutil.rmtree(root / "cases")
    (root / "report.html").unlink(missing_ok=True)
    return compact


def restore_run(root: str | Path) -> None:
    root = Path(root)
    compact = verify_compact_run(root, deep=True)
    cases = root / "cases"
    if cases.exists():
        raise ValueError(f"loose cases already exist: {root}")
    temporary = root / f".restore-{uuid.uuid4().hex}"
    temporary.mkdir()
    restored: dict[str, Path] = {}
    try:
        with tarfile.open(root / compact["archive"]["path"], "r:gz") as archive:
            for member in archive:
                destination = temporary / member.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                if member.isreg():
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValueError(f"unreadable compact archive member: {member.name}")
                    with destination.open("xb") as stream:
                        shutil.copyfileobj(source, stream)
                elif member.islnk():
                    target = restored.get(member.linkname)
                    if target is None:
                        raise ValueError(f"compact archive hard link has no target: {member.name}")
                    os.link(target, destination)
                else:
                    raise ValueError(f"unsupported compact archive member: {member.name}")
                restored[member.name] = destination
        (temporary / "cases").replace(cases)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    records = _source_records(root)
    if _tree_digest(records) != compact["archive"]["tree_sha256"]:
        shutil.rmtree(cases)
        raise ValueError(f"restored case tree failed verification: {root}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("compact", "verify", "restore"))
    parser.add_argument("run", type=Path, nargs="+")
    parser.add_argument("--keep-loose", action="store_true")
    parser.add_argument("--deep", action="store_true")
    args = parser.parse_args()
    for root in args.run:
        if args.action == "compact":
            result = compact_run(root, remove_loose=not args.keep_loose)
        elif args.action == "verify":
            result = verify_compact_run(root, deep=args.deep)
        else:
            restore_run(root)
            result = {"status": "restored"}
        print(json.dumps({"run": str(root), **result}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
