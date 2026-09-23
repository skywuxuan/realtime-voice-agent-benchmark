"""Stop benchmark services after their progress files seal a source-line boundary."""

import argparse
import json
import subprocess
import time
from pathlib import Path

from benchmark.contracts import pretty_json


def sealed_end(progress: Path) -> int:
    data = json.loads(progress.read_text(encoding="utf-8"))
    shards = data.get("shards") or []
    return shards[-1]["end_line"] if shards else 0


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(pretty_json(state), encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", action="append", nargs=3, metavar=("NAME", "UNIT", "PROGRESS"))
    parser.add_argument("--target-line", type=int, required=True)
    parser.add_argument("--poll-s", type=float, default=30)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    if not args.campaign or args.target_line < 1 or args.poll_s <= 0:
        parser.error("campaigns, a positive target line and positive polling are required")
    campaigns = {
        name: {"unit": unit, "progress": Path(progress), "stopped": False}
        for name, unit, progress in args.campaign
    }
    while not all(item["stopped"] for item in campaigns.values()):
        for name, item in campaigns.items():
            if item["stopped"] or not item["progress"].exists():
                continue
            end = sealed_end(item["progress"])
            item["sealed_end"] = end
            if end >= args.target_line:
                subprocess.run(
                    ["systemctl", "--user", "stop", item["unit"]],
                    check=True,
                    timeout=30,
                )
                item["stopped"] = True
                item["stop_reason"] = "sealed_source_line_target_reached"
        write_state(
            args.state,
            {
                "schema_version": "0.1",
                "kind": "campaign_stop_monitor",
                "target_line": args.target_line,
                "campaigns": {
                    name: {
                        "unit": item["unit"],
                        "progress": str(item["progress"]),
                        "sealed_end": item.get("sealed_end", 0),
                        "stopped": item["stopped"],
                        "stop_reason": item.get("stop_reason"),
                    }
                    for name, item in campaigns.items()
                },
            },
        )
        if not all(item["stopped"] for item in campaigns.values()):
            time.sleep(args.poll_s)


if __name__ == "__main__":
    main()
