"""Usage: python -m scenarios.validate case.yaml --asset-root ."""

import argparse
import json

from pydantic import ValidationError

from scenarios.loader import load_scenario, load_suite


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a scenario or suite without calling models"
    )
    parser.add_argument("path")
    parser.add_argument("--asset-root", default=".")
    parser.add_argument("--schema-only", action="store_true", help="Do not inspect WAV files")
    parser.add_argument("--suite", action="store_true")
    args = parser.parse_args()
    try:
        root = None if args.schema_only else args.asset_root
        if args.suite:
            _, scenarios = load_suite(args.path, asset_root=root)
        else:
            scenarios = (load_scenario(args.path, asset_root=root),)
        print(
            json.dumps(
                {
                    "valid": True,
                    "assets_verified": root is not None,
                    "scenarios": [
                        {"scenario_id": s.scenario_id, "sha256": s.sha256} for s in scenarios
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 0
    except ValidationError as error:
        print(
            json.dumps(
                {
                    "valid": False,
                    "errors": error.errors(include_input=False, include_context=False),
                },
                ensure_ascii=False,
            )
        )
    except (ValueError, OSError) as error:
        print(json.dumps({"valid": False, "error": str(error)}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
