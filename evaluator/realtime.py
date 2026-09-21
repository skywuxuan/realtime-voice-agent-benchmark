"""Deterministic latency evaluation from sealed recordings; no model calls."""

import json
import math
from collections import Counter
from pathlib import Path

from benchmark.config import LatencyProfile
from benchmark.contracts import content_hash
from events.replay import RecordingError, read_recording
from scenarios.schema import Scenario

EVALUATOR_VERSION = "latency-0.2"


def distribution(values: list[float], *, eligible_count: int, timeout_count: int) -> dict:
    values = sorted(values)
    result = {
        "unit": "ms",
        "n": len(values),
        "eligible_count": eligible_count,
        "timeout_count": timeout_count,
        "mean": sum(values) / len(values) if values else None,
        "quantile_method": "nearest_rank",
    }
    for q in (50, 90, 95, 99):
        result[f"p{q}"] = values[math.ceil(q / 100 * len(values)) - 1] if values else None
    return result


def evaluate_case(root: Path, *, profile: LatencyProfile | None = None) -> dict:
    result = {
        "scenario_id": root.parent.name,
        "attempt_id": root.name,
        "warmup": False,
        "status": "invalid",
        "reasons": [],
        "measurement_valid": False,
        "censored": False,
        "premature": False,
        "ttfa_receive_ms": None,
        "ttfa_playback_ms": None,
        "commit_to_audio_ms": None,
        "breakdown_ms": {},
        "evidence": {},
        "group": {"unclassified": True},
        "cleanup_warnings": [],
    }
    try:
        recording = read_recording(root, allow_partial=True)
        context = recording.manifest["context"]
        result.update(scenario_id=context["scenario_id"], attempt_id=context["attempt_id"])
        config = json.loads((root / "config.json").read_text())
        if config.get("mode") != "latency_benchmark":
            raise RecordingError("not_latency_benchmark")
        result["warmup"] = config["warmup"]
        scenario = Scenario.model_validate_json((root / "scenario.json").read_text()).model_dump(
            mode="json"
        )
        trial = json.loads((root / "trial.json").read_text())
        action = scenario["actions"][0]
        asset = scenario["audio"]["assets"][action["asset"]]
        annotation = config["boundary_annotation"]
        effective_profile = profile or LatencyProfile.model_validate(config["latency_profile"])
        input_asset = config.get("input_asset", {})
        renderer_profile = input_asset.get("derivation", {}).get("renderer_profile_id")
        result["input_asset"] = {
            "path": asset["path"],
            "sha256": asset["sha256"],
            "renderer_profile_id": renderer_profile,
        }
        result["group"] = {
            "model_config_sha256": config["model_config_sha256"],
            "model": config["model_config"]["model"],
            "voice": config["model_config"].get("voice"),
            "turn_mode": config["model_config"]["turn_mode"],
            "control_profile": config["model_config"]["control_profile"],
            "playback_mode": config["playback_mode"],
            "profile_sha256": content_hash(effective_profile.model_dump(mode="json")),
            "boundary_method": annotation["method"],
            "boundary_status": annotation["status"],
            "input_kind": config["input_kind"],
            "renderer_profile_id": renderer_profile,
        }
        if trial["status"] == "unsupported":
            result.update(status="unsupported", reasons=[trial["reason"]])
            return result
        if recording.manifest["status"] != "complete":
            raise RecordingError("incomplete_recording")
        events = recording.events
        if any(e.event == "session_end" and not e.payload.complete for e in events):
            fatal_errors = [e for e in events if e.event == "error" and e.payload.fatal]
            if not fatal_errors and trial["status"] in {"completed", "model_failed"}:
                # A missing post-observation close acknowledgement does not erase a
                # received first packet or an already observed first-audio timeout.
                result["cleanup_warnings"].append("session_close_not_acknowledged")
            else:
                result["reasons"].append("abnormal_session_end")
        case_ends = [e for e in events if e.event == "case_end"]
        if len(case_ends) != 1 or case_ends[0].payload.status != trial["status"]:
            raise RecordingError("case_outcome_mismatch")
        if trial["status"] in {"invalid", "infra_failed"}:
            result["reasons"].append(trial["reason"])
        sent = [e for e in events if e.event == "user_audio_chunk"]
        max_late = max(
            ((e.payload.send_started_ns - e.payload.planned_send_ns) / 1e6 for e in sent), default=0
        )
        max_send = max(
            ((e.payload.send_completed_ns - e.payload.send_started_ns) / 1e6 for e in sent),
            default=0,
        )
        max_play = max(
            (
                (e.payload.wake_lateness_ns or 0) / 1e6
                for e in events
                if e.event == "assistant_playback_chunk"
            ),
            default=0,
        )
        result["timing_diagnostics_ms"] = {
            "max_send_lateness": max_late,
            "max_send_duration": max_send,
            "max_playback_lateness": max_play,
        }
        if (
            max_late > effective_profile.max_send_lateness_ms
            or max_send > effective_profile.max_send_duration_ms
        ):
            result["reasons"].append("input_timing_out_of_bounds")
        if max_play > effective_profile.max_playback_lateness_ms:
            result["reasons"].append("playback_timing_out_of_bounds")
        if result["reasons"]:
            return result
        if annotation["method"] == "unverified" or annotation["status"] == "unverified":
            result.update(status="unknown", reasons=["unverified_speech_boundary"])
            return result
        user_ends = [
            e for e in events if e.event == "user_audio_end" and e.turn_id == action["turn_id"]
        ]
        if len(user_ends) != 1:
            raise RecordingError("missing_or_ambiguous_user_end")
        user_end = user_ends[0]
        if (
            user_end.payload.end_sample != asset["speech_bounds_samples"][1]
            or user_end.payload.annotation_source != annotation["method"]
        ):
            raise RecordingError("speech_annotation_mismatch")
        by_id = {event.event_id: event for event in events}
        boundary_chunk = by_id.get(user_end.causal_event_id)
        if boundary_chunk is None or boundary_chunk.event != "user_audio_chunk":
            raise RecordingError("speech_end_has_no_audio_handoff_evidence")
        reference = boundary_chunk.payload.audio_ref
        if (
            reference.sample_offset + reference.sample_count != user_end.payload.end_sample
            or boundary_chunk.payload.send_completed_ns != user_end.timestamp_monotonic_ns
        ):
            raise RecordingError("speech_end_does_not_match_audio_handoff")
        result["evidence"]["user_audio_end"] = user_end.event_id
        if trial["reason"] == "response_timeout":
            deadline = (
                user_end.timestamp_monotonic_ns
                + scenario["termination"]["response_timeout_ms"] * 1_000_000
            )
            if case_ends[0].timestamp_monotonic_ns < deadline:
                raise RecordingError("timeout_before_observation_deadline")
        responses = {
            e.response_id: e
            for e in events
            if e.event == "assistant_response_start"
            and e.turn_id == action["turn_id"]
            and e.payload.association_method != "ambiguous"
        }
        candidates = sorted(
            (
                e
                for e in events
                if e.event == "assistant_audio_start" and e.response_id in responses
            ),
            key=lambda e: e.timestamp_monotonic_ns,
        )
        result["measurement_valid"] = True
        if not candidates:
            if any(e.event == "assistant_audio_start" for e in events):
                result.update(
                    status="unknown",
                    measurement_valid=False,
                    reasons=["unassociated_audio_response"],
                )
            else:
                result.update(
                    status="fail",
                    censored=trial["reason"] == "response_timeout",
                    reasons=[trial["reason"] if trial["status"] != "completed" else "no_audio"],
                )
            return result
        first = candidates[0]
        first_chunk = by_id.get(first.payload.first_chunk_event_id)
        if (
            first_chunk is None
            or first_chunk.timestamp_monotonic_ns != first.timestamp_monotonic_ns
        ):
            raise RecordingError("audio_start_does_not_match_received_chunk")
        result["evidence"]["assistant_audio_start"] = first.event_id
        result["evidence"]["assistant_response_start"] = responses[first.response_id].event_id
        ttfa = (first.timestamp_monotonic_ns - user_end.timestamp_monotonic_ns) / 1e6
        result["ttfa_receive_ms"] = ttfa
        result["premature"] = ttfa < 0
        playback = next(
            (
                e
                for e in events
                if e.event == "assistant_playback_start" and e.response_id == first.response_id
            ),
            None,
        )
        if playback:
            if playback.timestamp_monotonic_ns < first.timestamp_monotonic_ns:
                raise RecordingError("playback_precedes_audio_reception")
            result["ttfa_playback_ms"] = (
                playback.timestamp_monotonic_ns - user_end.timestamp_monotonic_ns
            ) / 1e6
            result["evidence"]["assistant_playback_start"] = playback.event_id
            result["breakdown_ms"]["receive_to_playback"] = (
                playback.timestamp_monotonic_ns - first.timestamp_monotonic_ns
            ) / 1e6
        commit = next(
            (
                e
                for e in events
                if e.event == "user_turn_commit"
                and e.turn_id == action["turn_id"]
                and e.payload.phase == "requested"
            ),
            None,
        )
        if commit:
            result["commit_to_audio_ms"] = (
                first.timestamp_monotonic_ns - commit.timestamp_monotonic_ns
            ) / 1e6
            result["evidence"]["user_turn_commit"] = commit.event_id
        vad = next(
            (e for e in events if e.event == "vad_end" and e.turn_id == action["turn_id"]), None
        )
        response_start = responses[first.response_id]
        if (
            vad
            and user_end.timestamp_monotonic_ns
            <= vad.timestamp_monotonic_ns
            <= response_start.timestamp_monotonic_ns
            <= first.timestamp_monotonic_ns
        ):
            result["breakdown_ms"].update(
                client_observed_endpointing=(
                    vad.timestamp_monotonic_ns - user_end.timestamp_monotonic_ns
                )
                / 1e6,
                post_vad_to_response_start=(
                    response_start.timestamp_monotonic_ns - vad.timestamp_monotonic_ns
                )
                / 1e6,
                response_start_to_audio=(
                    first.timestamp_monotonic_ns - response_start.timestamp_monotonic_ns
                )
                / 1e6,
            )
            result["evidence"]["vad_end"] = vad.event_id
        if result["premature"]:
            result.update(status="fail", reasons=["premature_response"])
        elif ttfa > scenario["termination"]["response_timeout_ms"]:
            result.update(status="fail", reasons=["response_after_deadline"])
        elif trial["status"] != "completed":
            result.update(status="fail", reasons=[trial["reason"]])
        elif not playback:
            result.update(status="unknown", reasons=["missing_playback_observation"])
        else:
            result["status"] = "pass"
        return result
    except (RecordingError, OSError, KeyError, ValueError, TypeError) as error:
        result.update(
            status="invalid",
            measurement_valid=False,
            reasons=[f"artifact_error:{type(error).__name__}"],
        )
        return result


def _counts(cases: list[dict]) -> dict:
    counts = Counter(case["status"] for case in cases)
    attempted = len(cases) - counts["unsupported"]
    valid = counts["pass"] + counts["fail"] + counts["unknown"]
    return {
        "attempted": attempted,
        "valid": valid,
        "pass": counts["pass"],
        "fail": counts["fail"],
        "unknown": counts["unknown"],
        "invalid": counts["invalid"],
        "unsupported": counts["unsupported"],
        "timeout": sum(case["censored"] for case in cases),
        "premature": sum(case["premature"] for case in cases),
        "cleanup_warning_count": sum(bool(case.get("cleanup_warnings")) for case in cases),
        "case_success_rate": counts["pass"] / attempted if attempted else None,
        "valid_case_success_rate": counts["pass"] / valid if valid else None,
        "coverage": valid / attempted if attempted else None,
    }


def aggregate(cases: list[dict]) -> dict:
    scored = [case for case in cases if not case["warmup"]]
    groups: dict[str, list] = {}
    for case in scored:
        groups.setdefault(content_hash(case["group"]), []).append(case)
    output = []
    for group_id, rows in sorted(groups.items()):
        eligible = [row for row in rows if row["measurement_valid"]]
        summary = {"group_id": group_id, "dimensions": rows[0]["group"], "counts": _counts(rows)}
        for metric in ("ttfa_receive_ms", "ttfa_playback_ms", "commit_to_audio_ms"):
            samples = [
                {
                    "scenario_id": row["scenario_id"],
                    "attempt_id": row["attempt_id"],
                    "value_ms": row[metric],
                }
                for row in eligible
                if row[metric] is not None and row[metric] >= 0 and not row["premature"]
            ]
            summary[metric] = {
                **distribution(
                    [row["value_ms"] for row in samples],
                    eligible_count=len(eligible),
                    timeout_count=sum(row["censored"] for row in eligible),
                ),
                "samples": samples,
            }
        output.append(summary)
    return {
        "schema_version": "0.1",
        "evaluator_version": EVALUATOR_VERSION,
        "realtime": {
            "counts": _counts(scored),
            "warmup_count": len(cases) - len(scored),
            "groups": output,
            "cases": cases,
        },
        "agent": {"status": "not_run"},
        "response_quality": {"status": "not_run"},
    }
