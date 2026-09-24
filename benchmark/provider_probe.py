"""Capability probe for deferred providers; never guesses a wire protocol."""

import argparse
import json
from pathlib import Path

from adapters.registry import resolve_adapter
from benchmark.contracts import pretty_json


def probe(alias: str, output: Path) -> dict:
    registration = resolve_adapter(alias)
    live = alias in {"qwen-realtime", "step-realtime", "doubao-realtime"}
    result = {
        "schema_version": "0.1",
        "adapter": alias,
        "status": "live_supported" if live else "deferred",
        "credential_variables": registration.credential_variables,
        "config_path": str(registration.config_path),
        "capabilities": {"status": "unknown", "verification": "unverified"},
        "reason": "use the provider-specific benchmark for a real connection"
        if live
        else "official wire protocol must be verified before live adapter implementation",
    }
    output.mkdir(parents=True, exist_ok=False)
    (output / "probe.json").write_text(pretty_json(result), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        choices=["step-realtime", "doubao-realtime", "qwen-realtime"],
        required=True,
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(probe(args.model, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
