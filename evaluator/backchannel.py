"""Listener backchannel scoring with shared stimulus/timing validation."""

import json
from collections import Counter
from pathlib import Path

from benchmark.config import LatencyProfile
from evaluator.interruption import EXCLUDED
from evaluator.interruption import evaluate_case as evaluate_stimulus

EVALUATOR_VERSION = "backchannel-0.2"


def evaluate_case(root: Path, *, profile: LatencyProfile | None = None):
    result = evaluate_stimulus(root, profile=profile, _backchannel=True)
    for key in (
        "interruption_detected",
        "evidence_level",
        "stop_latency_ms",
        "stop_censored",
        "residual_audio_duration_ms",
        "residual_censored",
        "context_switch",
        "context_switch_basis",
    ):
        result.pop(key, None)
    result.setdefault("false_interruption", None)
    result.setdefault("response_continued", None)
    return result


def aggregate(cases):
    scored = [row for row in cases if not row["warmup"]]

    def eligible(row):
        return row["eligible"] and row["status"] not in EXCLUDED

    def counts(rows):
        tally = Counter(row["status"] for row in rows)
        return {
            "attempted": len(rows),
            "eligible": sum(eligible(row) for row in rows),
            **{
                k: tally[k]
                for k in ("pass", "fail", "unknown", "invalid", "infra_failed", "unsupported")
            },
        }

    groups = {}
    for row in scored:
        groups.setdefault(
            json.dumps(row.get("group", {}), sort_keys=True, ensure_ascii=False), []
        ).append(row)
    summaries = []
    for _, rows in sorted(groups.items()):
        valid = [row for row in rows if eligible(row)]
        known = [row for row in valid if row["false_interruption"] is not None]
        false = sum(row["false_interruption"] for row in known)
        n = len(valid)
        summaries.append(
            {
                "dimensions": rows[0].get("group", {}),
                "counts": counts(rows),
                "false_interruption_rate": false / len(known) if known else None,
                "false_interruption_known_count": len(known),
                "false_interruption_unknown_count": n - len(known),
                "false_interruption_rate_bounds": [false / n, (false + n - len(known)) / n]
                if n
                else None,
                "response_continuation_rate": sum(
                    row["response_continued"] is True for row in valid
                )
                / n
                if n
                else None,
                "cases": rows,
            }
        )
    return {
        "schema_version": "0.1",
        "evaluator_version": EVALUATOR_VERSION,
        "realtime": {
            "suite": "backchannel",
            "counts": {**counts(scored), "warmup_count": len(cases) - len(scored)},
            "groups": summaries,
            "cases": cases,
        },
        "agent": {"status": "not_run"},
        "response_quality": {"status": "not_run"},
    }
