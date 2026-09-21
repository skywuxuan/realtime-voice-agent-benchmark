"""Offline annotated pause/turn-taking/overlap measurements from sealed recordings."""

import json
from collections import Counter
from pathlib import Path
from typing import Iterable

from benchmark.config import LatencyProfile
from benchmark.contracts import content_hash
from evaluator.realtime import distribution
from events.replay import RecordingError, read_recording
from events.schema import EventDraft

EVALUATOR_VERSION = "duplex-0.2"
EXCLUDED = {"invalid", "infra_failed", "unsupported"}


def _interval(event: EventDraft) -> tuple[int, int]:
    ref = event.payload.audio_ref
    duration = ref.sample_count * 1_000_000_000 // ref.sample_rate_hz
    if event.event == "user_audio_chunk":
        # The chunk timestamp marks handoff completion, not the beginning of its samples.
        end = event.payload.send_completed_ns
        return end - duration, end
    return event.timestamp_monotonic_ns, event.timestamp_monotonic_ns + duration


def _union(intervals):
    merged = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _intersection_ms(left, right):
    return (
        sum(max(0, min(b, d) - max(a, c)) for a, b in _union(left) for c, d in _union(right)) / 1e6
    )


def measure_events(
    events: Iterable[EventDraft], *, category: str, turn_id: str | None = None
) -> dict:
    rows = sorted(events, key=lambda e: (e.timestamp_monotonic_ns, e.event_id))
    if category not in {"turn_taking", "pause", "overlap"}:
        raise ValueError("unsupported duplex category")
    ends = [
        e for e in rows if e.event == "user_audio_end" and (turn_id is None or e.turn_id == turn_id)
    ]
    if len(ends) != 1:
        raise ValueError("one complete annotated utterance required")
    end = ends[0]
    starts = [
        e
        for e in rows
        if e.event == "assistant_response_start"
        and e.turn_id == end.turn_id
        and e.payload.association_method != "ambiguous"
    ]
    rids = {e.response_id for e in starts}
    audio = [e for e in rows if e.event == "assistant_audio_start" and e.response_id in rids]
    playback = [e for e in rows if e.event == "assistant_playback_start" and e.response_id in rids]

    def delta(values):
        return (
            (values[0].timestamp_monotonic_ns - end.timestamp_monotonic_ns) / 1e6
            if values
            else None
        )

    premature = any(e.timestamp_monotonic_ns < end.timestamp_monotonic_ns for e in starts + audio)
    result = {
        "category": category,
        "premature_response": premature,
        "response_gap_ms": delta(starts),
        "ttfa_receive_ms": delta(audio),
        "ttfa_playback_ms": delta(playback),
        "response_count": len(starts),
        "turn_completion": bool(audio) and not premature and len(starts) == 1,
        "evidence": {
            "user_audio_end": end.event_id,
            "responses": [e.event_id for e in starts],
            "first_audio": audio[0].event_id if audio else None,
        },
    }
    markers = {}
    for event in rows:
        if (
            event.event in {"input_region_start", "input_region_end"}
            and event.turn_id == end.turn_id
        ):
            key = (event.payload.region_id, event.payload.kind)
            markers.setdefault(key, {})[event.event] = event
    windows = []
    for (region_id, kind), pair in sorted(markers.items()):
        if set(pair) != {"input_region_start", "input_region_end"}:
            raise ValueError("incomplete input region")
        left, right = pair["input_region_start"], pair["input_region_end"]
        if right.timestamp_monotonic_ns <= left.timestamp_monotonic_ns:
            raise ValueError("nonpositive input region duration")
        windows.append(
            {
                "region_id": region_id,
                "kind": kind,
                "start_ns": left.timestamp_monotonic_ns,
                "end_ns": right.timestamp_monotonic_ns,
                "duration_ms": (right.timestamp_monotonic_ns - left.timestamp_monotonic_ns) / 1e6,
                "premature_response": any(
                    left.timestamp_monotonic_ns
                    <= e.timestamp_monotonic_ns
                    < right.timestamp_monotonic_ns
                    for e in starts
                ),
                "evidence": [left.event_id, right.event_id],
            }
        )
    if category == "pause":
        pauses = [w for w in windows if w["kind"] == "pause"]
        if not pauses:
            raise ValueError("pause annotations required")
        result["pause_windows"] = pauses
        result["premature_response_rate"] = sum(w["premature_response"] for w in pauses) / len(
            pauses
        )
    if category == "overlap":
        interference = [w for w in windows if w["kind"] == "interferer"]
        if not interference:
            raise ValueError("interference annotations required")
        users = [
            _interval(e) for e in rows if e.event == "user_audio_chunk" and not e.payload.silence
        ]
        played = [_interval(e) for e in rows if e.event == "assistant_playback_chunk"]
        background = [(w["start_ns"], w["end_ns"]) for w in interference]
        result.update(
            overlap_duration_ms=_intersection_ms(users, played),
            interference_playback_overlap_ms=_intersection_ms(background, played),
            interference_windows=interference,
            overlap_time_basis="client_audio_handoff_and_virtual_playback",
            content_robustness="unknown",
        )
    return result


def evaluate_case(root: Path, *, profile: LatencyProfile | None = None) -> dict:
    result = {
        "scenario_id": root.parent.name,
        "attempt_id": root.name,
        "warmup": False,
        "category": None,
        "status": "invalid",
        "eligible": False,
        "censored": False,
        "reasons": [],
        "measurement": {},
        "cleanup_warnings": [],
    }
    try:
        recording = read_recording(root, allow_partial=True)

        def load(name):
            if name not in recording.manifest["files"]:
                raise RecordingError("unsealed evaluation input")
            return json.loads((root / name).read_text())

        config, scenario, trial = load("config.json"), load("scenario.json"), load("trial.json")
        if config.get("mode") != "duplex_benchmark":
            raise RecordingError("not_duplex_benchmark")
        result.update(
            warmup=config["warmup"],
            category=scenario["category"],
            **{k: recording.manifest["context"][k] for k in ("scenario_id", "attempt_id")},
        )
        if content_hash(scenario) != config["scenario_sha256"]:
            raise RecordingError("scenario hash mismatch")
        if recording.manifest["status"] != "complete":
            raise RecordingError("incomplete_recording")
        if trial["status"] in EXCLUDED:
            result.update(status=trial["status"], reasons=[trial["reason"]])
            return result
        events = recording.events
        if any(e.event == "error" and e.payload.fatal for e in events):
            result.update(status="infra_failed", reasons=["fatal_transport_event"])
            return result
        if any(e.event == "session_end" and not e.payload.complete for e in events):
            result["cleanup_warnings"].append("session_close_not_acknowledged")
        profile = profile or LatencyProfile.model_validate(config["latency_profile"])
        action = scenario["actions"][0]
        asset = scenario["audio"]["assets"][action["asset"]]
        annotation = asset["boundary_annotation"]
        input_asset = config.get("input_asset", {})
        renderer_profile = input_asset.get("derivation", {}).get("renderer_profile_id")
        result["input_asset"] = {
            "path": asset["path"],
            "sha256": asset["sha256"],
            "renderer_profile_id": renderer_profile,
        }
        result["group"] = {
            "model_config": config["model_config"],
            "category": scenario["category"],
            "profile": profile.model_dump(mode="json"),
            "playback_mode": config["playback_mode"],
            "boundary_method": annotation["method"],
            "boundary_status": annotation["status"],
            "input_kind": asset["provenance"]["kind"],
            "renderer_profile_id": renderer_profile,
            "interference_subtypes": sorted(
                {r["subtype"] for r in asset.get("regions", []) if r["kind"] == "interferer"}
            ),
            "pause_lengths_ms": [
                (r["bounds_samples"][1] - r["bounds_samples"][0]) * 1000 / asset["sample_rate_hz"]
                for r in asset.get("regions", [])
                if r["kind"] == "pause"
            ],
        }
        by_id = {e.event_id: e for e in events}
        chunks = [
            e for e in events if e.event == "user_audio_chunk" and e.turn_id == action["turn_id"]
        ]
        if not chunks:
            raise RecordingError("missing input chunks")
        if any(
            e.payload.send_started_ns - e.payload.planned_send_ns
            > profile.max_send_lateness_ms * 1e6
            or e.payload.send_completed_ns - e.payload.send_started_ns
            > profile.max_send_duration_ms * 1e6
            for e in chunks
        ):
            raise RecordingError("input_timing_out_of_bounds")
        if any(
            (e.payload.wake_lateness_ns or 0) > profile.max_playback_lateness_ms * 1e6
            for e in events
            if e.event == "assistant_playback_chunk"
        ):
            raise RecordingError("playback_timing_out_of_bounds")
        ends = [e for e in events if e.event == "user_audio_end" and e.turn_id == action["turn_id"]]
        if len(ends) != 1 or ends[0].payload.end_sample != asset["speech_bounds_samples"][1]:
            raise RecordingError("invalid utterance boundary")
        # Validate sample coverage and handoff evidence, without assuming record seq is clock order.
        cursor = chunks[0].payload.audio_ref.sample_offset
        base = cursor
        for chunk in chunks:
            ref = chunk.payload.audio_ref
            if ref.sample_offset != cursor:
                raise RecordingError("input sample coverage gap")
            cursor += ref.sample_count
        boundary_chunk = by_id.get(ends[0].causal_event_id)
        if (
            boundary_chunk not in chunks
            or boundary_chunk.payload.audio_ref.sample_offset
            + boundary_chunk.payload.audio_ref.sample_count
            - base
            != asset["speech_bounds_samples"][1]
        ):
            raise RecordingError("speech end lacks matching handoff")
        if ends[0].timestamp_monotonic_ns != boundary_chunk.payload.send_completed_ns:
            raise RecordingError("speech end time mismatch")
        for region in asset.get("regions", []):
            for kind, sample in zip(
                ("input_region_start", "input_region_end"), region["bounds_samples"]
            ):
                anchors = [
                    e
                    for e in events
                    if e.event == kind
                    and e.turn_id == action["turn_id"]
                    and e.payload.region_id == region["region_id"]
                ]
                if (
                    len(anchors) != 1
                    or anchors[0].payload.kind != region["kind"]
                    or anchors[0].payload.sample_index != sample
                ):
                    raise RecordingError("input region does not match scenario")
                if sample:
                    cause = by_id.get(anchors[0].causal_event_id)
                    if (
                        cause not in chunks
                        or cause.payload.audio_ref.sample_offset
                        + cause.payload.audio_ref.sample_count
                        - base
                        != sample
                        or anchors[0].timestamp_monotonic_ns != cause.payload.send_completed_ns
                    ):
                        raise RecordingError("input region lacks matching audio handoff")
        if annotation["status"] == "unverified":
            result.update(status="unknown", reasons=["unverified_annotation"])
            return result
        measurement = measure_events(
            events, category=scenario["category"], turn_id=action["turn_id"]
        )
        result.update(eligible=True, measurement=measurement)
        if trial["reason"] == "response_timeout":
            case_end = next(e for e in events if e.event == "case_end")
            if (
                case_end.timestamp_monotonic_ns
                < ends[0].timestamp_monotonic_ns
                + scenario["termination"]["response_timeout_ms"] * 1_000_000
            ):
                raise RecordingError("timeout before deadline")
            result["censored"] = True
        if measurement["premature_response"]:
            result.update(status="fail", reasons=["premature_response"])
        elif trial["status"] == "model_failed":
            result.update(status="fail", reasons=[trial["reason"]])
        elif measurement["turn_completion"]:
            if scenario["category"] == "overlap":
                result.update(status="unknown", reasons=["content_robustness_not_evaluated"])
            else:
                result["status"] = "pass"
        else:
            result.update(status="unknown", reasons=["missing_or_ambiguous_response"])
        return result
    except (RecordingError, OSError, KeyError, TypeError, ValueError, StopIteration) as error:
        result.update(status="invalid", eligible=False)
        result["reasons"].append(f"artifact_error:{type(error).__name__}:{str(error)}")
        return result


def aggregate(cases):
    scored = [r for r in cases if not r["warmup"]]

    def counts(rows):
        tally = Counter(r["status"] for r in rows)
        return {
            "attempted": len(rows),
            "eligible": sum(r["eligible"] and r["status"] not in EXCLUDED for r in rows),
            **{
                k: tally[k]
                for k in ("pass", "fail", "unknown", "invalid", "infra_failed", "unsupported")
            },
        }

    groups = {}
    for row in scored:
        key = json.dumps(row.get("group", {}), sort_keys=True, ensure_ascii=False)
        groups.setdefault(key, []).append(row)
    summaries = []
    for _, rows in sorted(groups.items()):
        eligible = [r for r in rows if r["eligible"] and r["status"] not in EXCLUDED]
        n = len(eligible)
        gaps = [
            r["measurement"]["ttfa_receive_ms"]
            for r in eligible
            if not r["measurement"]["premature_response"]
            and r["measurement"]["ttfa_receive_ms"] is not None
        ]
        summaries.append(
            {
                "dimensions": rows[0].get("group", {}),
                "counts": counts(rows),
                "premature_response_rate": sum(
                    r["measurement"]["premature_response"] for r in eligible
                )
                / n
                if n
                else None,
                "turn_completion_accuracy": sum(
                    r["measurement"]["turn_completion"] for r in eligible
                )
                / n
                if n
                else None,
                "ttfa_receive_ms": distribution(
                    gaps, eligible_count=n, timeout_count=sum(r["censored"] for r in eligible)
                ),
                "case_success_rate": sum(r["status"] == "pass" for r in eligible) / n
                if n
                else None,
                "cases": rows,
            }
        )
    return {
        "schema_version": "0.1",
        "evaluator_version": EVALUATOR_VERSION,
        "realtime": {
            "suite": "duplex",
            "counts": {**counts(scored), "warmup_count": len(cases) - len(scored)},
            "groups": summaries,
            "cases": cases,
        },
        "agent": {"status": "not_run"},
        "response_quality": {"status": "not_run"},
    }
