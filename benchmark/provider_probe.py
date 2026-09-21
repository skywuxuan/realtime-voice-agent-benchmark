"""Capability probe for deferred providers; never guesses a wire protocol."""

import argparse
import json
from pathlib import Path

from adapters.registry import resolve_adapter
from benchmark.contracts import canonical_json


def probe(alias: str, output: Path) -> dict:
    registration = resolve_adapter(alias)
    result = {
        "schema_version": "0.1",
        "adapter": alias,
        "status": "deferred" if alias != "qwen-realtime" else "live_supported",
        "credential_variables": registration.credential_variables,
        "config_path": str(registration.config_path),
        "capabilities": {"status": "unknown", "verification": "unverified"},
        "reason": "official wire protocol must be verified before live adapter implementation"
        if alias != "qwen-realtime"
        else "use benchmark.qwen_probe for a real Qwen connection",
    }
    output.mkdir(parents=True, exist_ok=False)
    (output / "probe.json").write_text(canonical_json(result) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        choices=["step-realtime", "doubao-realtime", "qwen3-omni-local", "qwen-realtime"],
        required=True,
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(probe(args.model, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
