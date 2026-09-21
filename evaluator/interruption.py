"""Offline barge-in evaluation; no vendor SDK or live model requests."""

import json
import math
from collections import Counter
from pathlib import Path

from benchmark.config import LatencyProfile
from benchmark.contracts import content_hash
from events.replay import RecordingError, read_recording

EVALUATOR_VERSION = "interruption-0.3"
EXCLUDED = {"invalid", "infra_failed", "unsupported"}


def _duration(event):
    ref = event.payload.audio_ref
    return ref.sample_count * 1_000_000_000 / ref.sample_rate_hz


def _played(events, rid, start, end):
    return sum(
        max(
            0,
            min(e.timestamp_monotonic_ns + _duration(e), end)
            - max(e.timestamp_monotonic_ns, start),
        )
        for e in events
        if e.event == "assistant_playback_chunk" and e.response_id == rid
    )


def evaluate_case(root: Path, *, profile: LatencyProfile | None = None, _backchannel=False) -> dict:
    result = {
        "scenario_id": root.parent.name,
        "attempt_id": root.name,
        "warmup": False,
        "status": "invalid",
        "reasons": [],
        "eligible": False,
        "interruption_detected": False,
        "evidence_level": None,
        "stop_latency_ms": None,
        "stop_censored": False,
        "residual_audio_duration_ms": None,
        "residual_censored": False,
        "context_switch": "unknown",
        "context_switch_basis": "spoken_transcript_rule",
        "cleanup_warnings": [],
        "evidence": {},
    }
    try:
        recording = read_recording(root, allow_partial=True)
        if (
            recording.manifest["status"] == "complete"
            and not {"config.json", "trial.json", "scenario.json"}
            <= recording.manifest["files"].keys()
        ):
            raise RecordingError("unsealed_evaluation_input")
        config = json.loads((root / "config.json").read_text())
        result["warmup"] = config["warmup"]
        mode = "backchannel_benchmark" if _backchannel else "interruption_benchmark"
        if config.get("mode") != mode:
            raise RecordingError("not_interruption_benchmark")
        trial = json.loads((root / "trial.json").read_text())
        scenario = json.loads((root / "scenario.json").read_text())
        if content_hash(scenario) != config["scenario_sha256"]:
            raise RecordingError("scenario_hash_mismatch")
        effective_profile = profile or LatencyProfile.model_validate(config["latency_profile"])
        input_assets = config.get("input_assets", {})
        renderer_profiles = sorted(
            {
                value.get("derivation", {}).get("renderer_profile_id")
                for value in input_assets.values()
                if value.get("derivation", {}).get("renderer_profile_id")
            }
        )
        result["input_assets"] = {
            key: {
                "path": value.get("path"),
                "sha256": value.get("sha256"),
                "renderer_profile_id": value.get("derivation", {}).get("renderer_profile_id"),
            }
            for key, value in input_assets.items()
        }
        result["group"] = {
            "model_config": config["model_config"],
            "control_profile": config["control_profile"],
            "playback_mode": config["playback_mode"],
            "profile": effective_profile.model_dump(mode="json"),
            "boundary_methods": {
                role: {key: annotation.get(key) for key in ("method", "status", "resolution_ms")}
                for role, annotation in config["boundary_annotations"].items()
            },
            "renderer_profile_ids": renderer_profiles,
        }
        events = recording.events
        result["cleanup_warnings"] = sorted(set(trial.get("cleanup_warnings", [])))
        if any(e.event == "session_end" and not e.payload.complete for e in events):
            result["cleanup_warnings"] = sorted(
                set(result["cleanup_warnings"] + ["session_close_not_acknowledged"])
            )
        if recording.manifest["status"] != "complete":
            if trial["status"] == "infra_failed":
                result["status"] = "infra_failed"
            result["reasons"].append("incomplete_recording")
            return result
        if trial["status"] in EXCLUDED:
            result.update(status=trial["status"], reasons=[trial["reason"]])
            return result
        if any(e.event == "error" and e.payload.fatal for e in events):
            result.update(status="infra_failed", reasons=["fatal_transport_event"])
            return result
        if any(
            e.event == "interrupt_requested"
            or (e.event == "assistant_cancelled" and e.payload.initiator == "client")
            for e in events
        ):
            result["reasons"].append("client_control_in_native_trial")
            return result
        maxima = {
            "send_lateness_ms": max(
                (
                    max(0, e.payload.send_started_ns - e.payload.planned_send_ns) / 1e6
                    for e in events
                    if e.event == "user_audio_chunk"
                ),
                default=0,
            ),
            "send_duration_ms": max(
                (
                    (e.payload.send_completed_ns - e.payload.send_started_ns) / 1e6
                    for e in events
                    if e.event == "user_audio_chunk"
                ),
                default=0,
            ),
            "playback_lateness_ms": max(
                (
                    (e.payload.wake_lateness_ns or 0) / 1e6
                    for e in events
                    if e.event == "assistant_playback_chunk"
                ),
                default=0,
            ),
        }
        result["timing"] = maxima
        if (
            maxima["send_lateness_ms"] > effective_profile.max_send_lateness_ms
            or maxima["send_duration_ms"] > effective_profile.max_send_duration_ms
            or maxima["playback_lateness_ms"] > effective_profile.max_playback_lateness_ms
        ):
            result["reasons"].append("timing_out_of_bounds")
            return result
        marker_kind = "backchannel_start" if _backchannel else "interrupt_start"
        markers = [e for e in events if e.event == marker_kind]
        if len(markers) != 1:
            result["reasons"].append("missing_or_duplicate_stimulus")
            return result
        interrupt = markers[0]
        onset, rid = interrupt.timestamp_monotonic_ns, interrupt.response_id
        action = next(
            a for a in scenario["actions"] if a["action_id"] == interrupt.payload.action_id
        )
        user_start = next(
            (e for e in events if e.event == "user_audio_start" and e.turn_id == action["turn_id"]),
            None,
        )
        user_end = next(
            (e for e in events if e.event == "user_audio_end" and e.turn_id == action["turn_id"]),
            None,
        )
        if not user_start or not user_end or user_start.timestamp_monotonic_ns != onset:
            result["reasons"].append("incomplete_stimulus_audio")
            return result
        old_start = next(
            (e for e in events if e.event == "assistant_playback_start" and e.response_id == rid),
            None,
        )
        old_end = next(
            (e for e in events if e.event == "assistant_response_end" and e.response_id == rid),
            None,
        )
        old_stops = [
            e for e in events if e.event == "assistant_playback_stop" and e.response_id == rid
        ]
        received_ns = sum(
            _duration(e)
            for e in events
            if e.event == "assistant_audio_chunk"
            and e.response_id == rid
            and e.timestamp_monotonic_ns <= onset
        )
        dropped_ns = sum(
            _duration(e)
            for e in events
            if e.event == "audio_chunk_dropped"
            and e.response_id == rid
            and e.timestamp_monotonic_ns <= onset
        )
        remaining = max(0, received_ns - dropped_ns - _played(events, rid, 0, onset)) / 1e6
        result["stimulus_buffer_remaining_ms"] = remaining
        playing = bool(
            old_start
            and old_start.timestamp_monotonic_ns <= onset
            and remaining > 0
            and not any(e.timestamp_monotonic_ns <= onset for e in old_stops)
        )
        checks = []
        for precondition in action.get("preconditions", []):
            kind = precondition["type"]
            if kind == "minimum_continuation_evidence":
                checks.append(remaining + 1e-6 >= precondition["remaining_ms"])
            elif kind == "response_still_generating":
                checks.append(old_end is None or old_end.timestamp_monotonic_ns > onset)
            else:
                checks.append(playing)
        if not playing or not all(checks):
            result["reasons"].append("stimulus_precondition_not_met")
            return result
        observation_end = next(
            (
                e
                for e in events
                if e.event == "scenario_action_end" and e.payload.action_id == action["action_id"]
            ),
            None,
        )
        case_end = next((e for e in reversed(events) if e.event == "case_end"), None)
        horizon = (observation_end or case_end).timestamp_monotonic_ns
        result["eligible"] = True
        if _backchannel:
            return _backchannel_outcome(
                events, result, interrupt, user_end, old_end, horizon, trial
            )
        result["evidence"].update(
            interrupt_start=interrupt.event_id, old_playback_start=old_start.event_id
        )
        by_id = {e.event_id: e for e in events}
        cancellations = [
            e
            for e in events
            if e.event == "assistant_cancelled"
            and e.response_id == rid
            and e.payload.initiator == "server"
            and onset <= e.timestamp_monotonic_ns <= horizon
        ]
        detections = []
        for event in events:
            if (
                event.event != "interrupt_detected"
                or event.response_id != rid
                or not onset <= event.timestamp_monotonic_ns <= horizon
            ):
                continue
            evidence = [by_id[i] for i in event.payload.evidence_event_ids]
            if (
                event.payload.evidence_level == "confirmed"
                and cancellations
                and any(
                    e.event == "vad_start" and onset <= e.timestamp_monotonic_ns <= horizon
                    for e in evidence
                )
                and any(
                    e.event == "assistant_response_end"
                    and e.response_id == rid
                    and e.payload.status == "cancelled"
                    for e in evidence
                )
            ):
                detections.append(event)
        if detections:
            result.update(interruption_detected=True, evidence_level="confirmed")
            result["evidence"]["interrupt_detected"] = detections[0].event_id
        stop = next(
            (
                e
                for e in old_stops
                if onset <= e.timestamp_monotonic_ns <= horizon
                and e.payload.stop_reason != "case_abort"
            ),
            None,
        )
        result["stop_censored"] = stop is None
        result["residual_censored"] = stop is None
        if stop:
            result["stop_latency_ms"] = (stop.timestamp_monotonic_ns - onset) / 1e6
            result["evidence"]["old_playback_stop"] = stop.event_id
        else:
            result["reasons"].append("old_playback_stop_not_observed")
        result["residual_audio_duration_ms"] = (
            _played(events, rid, onset, stop.timestamp_monotonic_ns if stop else horizon) / 1e6
        )
        new_starts = [
            e
            for e in events
            if e.event == "assistant_response_start"
            and e.response_id != rid
            and e.timestamp_monotonic_ns >= onset
            and e.turn_id == action["turn_id"]
            and e.payload.association_method != "ambiguous"
        ]
        new_start = new_starts[0] if len(new_starts) == 1 else None
        result["context_switch_text_rule"] = "unknown"
        if new_start:
            new_id = new_start.response_id
            result["evidence"]["new_response_start"] = new_start.event_id
            new_end = next(
                (
                    e
                    for e in events
                    if e.event == "assistant_response_end" and e.response_id == new_id
                ),
                None,
            )
            chunks = [
                e for e in events if e.event == "assistant_audio_chunk" and e.response_id == new_id
            ]
            played = sum(
                _duration(e)
                for e in events
                if e.event == "assistant_playback_chunk" and e.response_id == new_id
            )
            received = sum(_duration(e) for e in chunks)
            playback_done = any(
                e.event == "assistant_playback_stop"
                and e.response_id == new_id
                and e.payload.stop_reason == "completed"
                for e in events
            )
            text = "".join(
                e.payload.text
                for e in events
                if e.event == "assistant_text_done"
                and e.response_id == new_id
                and e.payload.channel == "spoken_transcript"
                and not e.payload.partial
            )
            expected = scenario["oracle"].get("expected_new_intent", {})
            forbidden = scenario["oracle"].get("forbidden_old_intent", {})
            if (
                text
                and expected
                and all(str(v) in text for v in expected.values())
                and not any(str(v) in text for v in forbidden.values())
            ):
                result["context_switch_text_rule"] = "pass"
            if not new_end:
                result["reasons"].append("new_response_completion_missing")
            elif new_end.payload.status != "completed":
                result["reasons"].append("new_response_not_completed")
            elif not chunks:
                result["reasons"].append("new_response_no_audio")
            elif not playback_done or abs(played - received) > 1:
                result["reasons"].append("new_response_playback_incomplete")
            else:
                result["context_switch"] = result["context_switch_text_rule"]
                if result["context_switch"] == "unknown":
                    result["reasons"].append("context_switch_rule_inconclusive")
        else:
            result["reasons"].append("new_response_not_uniquely_associated")
        if trial["status"] == "model_failed":
            result.update(status="fail")
            result["reasons"].append(trial["reason"])
        elif stop is None or (
            new_start
            and any(
                reason in result["reasons"]
                for reason in (
                    "new_response_completion_missing",
                    "new_response_not_completed",
                    "new_response_no_audio",
                )
            )
        ):
            result["status"] = "fail"
        elif result["interruption_detected"] and result["context_switch"] == "pass":
            result["status"] = "pass"
        else:
            result["status"] = "unknown"
        return result
    except (
        RecordingError,
        OSError,
        KeyError,
        TypeError,
        ValueError,
        AttributeError,
        StopIteration,
    ) as error:
        result.update(status="invalid", eligible=False)
        result["reasons"].append(f"artifact_error:{type(error).__name__}")
        return result


def _backchannel_outcome(events, result, marker, user_end, old_end, horizon, trial):
    rid, onset = marker.response_id, marker.timestamp_monotonic_ns
    result["evidence"] = {"backchannel_start": marker.event_id, "user_audio_end": user_end.event_id}
    cancels = [
        e
        for e in events
        if e.event == "assistant_cancelled"
        and e.response_id == rid
        and e.payload.initiator == "server"
        and onset <= e.timestamp_monotonic_ns <= horizon
    ]
    if cancels:
        result.update(status="fail", false_interruption=True, response_continued=False)
        result["evidence"]["server_cancelled"] = cancels[0].event_id
        return result
    result.update(status="unknown", false_interruption=None, response_continued=None)
    if not old_end or old_end.payload.status != "completed":
        result["reasons"].append("missing_normal_response_completion")
        return result
    received = sum(
        _duration(e) for e in events if e.event == "assistant_audio_chunk" and e.response_id == rid
    )
    played = sum(
        _duration(e)
        for e in events
        if e.event == "assistant_playback_chunk" and e.response_id == rid
    )
    stop = next(
        (
            e
            for e in events
            if e.event == "assistant_playback_stop"
            and e.response_id == rid
            and e.payload.stop_reason == "completed"
        ),
        None,
    )
    continuation = _played(
        events,
        rid,
        user_end.timestamp_monotonic_ns,
        stop.timestamp_monotonic_ns if stop else horizon,
    )
    result["continuation_audio_ms"] = continuation / 1e6
    if not stop or abs(received - played) > 1 or continuation <= 0:
        result["reasons"].append("continuation_not_fully_observed")
        return result
    # A replacement response is not continuation of the bound old response.
    if any(
        e.event == "assistant_response_start"
        and e.response_id != rid
        and onset <= e.timestamp_monotonic_ns <= horizon
        for e in events
    ):
        result["reasons"].append("new_response_during_backchannel")
        return result
    if trial["status"] == "model_failed":
        result["reasons"].append(trial["reason"])
        return result
    result.update(status="pass", false_interruption=False, response_continued=True)
    result["evidence"]["response_end"] = old_end.event_id
    return result


def _eligible(row):
    return row["eligible"] and row["status"] not in EXCLUDED


def _stats(values, denominator, censored=0):
    values = sorted(values)
    result = {
        "unit": "ms",
        "n": len(values),
        "eligible_count": denominator,
        "censored_count": censored,
        "mean": sum(values) / len(values) if values else None,
    }
    for p in (50, 90, 95, 99):
        result[f"p{p}"] = values[math.ceil(p / 100 * len(values)) - 1] if values else None
    return result


def _counts(rows):
    counts = Counter(row["status"] for row in rows)
    return {
        "attempted": len(rows),
        "eligible": sum(_eligible(row) for row in rows),
        **{
            k: counts[k]
            for k in ("pass", "fail", "unknown", "invalid", "infra_failed", "unsupported")
        },
    }


def aggregate(cases: list[dict]) -> dict:
    scored = [case for case in cases if not case["warmup"]]
    groups = {}
    for case in scored:
        key = json.dumps(case.get("group", {}), ensure_ascii=False, sort_keys=True)
        groups.setdefault(key, []).append(case)
    summaries = []
    for _, rows in sorted(groups.items()):
        eligible = [row for row in rows if _eligible(row)]
        denominator = len(eligible)
        context_known = [row for row in eligible if row["context_switch"] in {"pass", "fail"}]
        summaries.append(
            {
                "dimensions": rows[0].get("group", {}),
                "counts": _counts(rows),
                "interruption_detection_rate": sum(row["interruption_detected"] for row in eligible)
                / denominator
                if denominator
                else None,
                "stop_latency_ms": _stats(
                    [
                        row["stop_latency_ms"]
                        for row in eligible
                        if row["stop_latency_ms"] is not None and row["stop_latency_ms"] >= 0
                    ],
                    denominator,
                    sum(row.get("stop_censored", False) for row in eligible),
                ),
                "residual_audio_duration_ms": _stats(
                    [
                        row["residual_audio_duration_ms"]
                        for row in eligible
                        if row["residual_audio_duration_ms"] is not None
                        and not row.get("residual_censored", False)
                    ],
                    denominator,
                    sum(row.get("residual_censored", False) for row in eligible),
                ),
                "false_interruption_rate": None,
                "context_switch_accuracy": sum(
                    row["context_switch"] == "pass" for row in context_known
                )
                / len(context_known)
                if context_known
                else None,
                "context_switch_known_count": len(context_known),
                "context_switch_unknown_count": denominator - len(context_known),
                "case_success_rate": sum(row["status"] == "pass" for row in eligible) / denominator
                if denominator
                else None,
                "cases": rows,
            }
        )
    return {
        "schema_version": "0.1",
        "evaluator_version": EVALUATOR_VERSION,
        "realtime": {
            "suite": "interruption",
            "counts": {**_counts(scored), "warmup_count": len(cases) - len(scored)},
            "groups": summaries,
            "cases": cases,
        },
        "agent": {"status": "not_run"},
        "response_quality": {"status": "not_run"},
    }
