"""Offline interruption evaluation entry point."""

import argparse
import json
from pathlib import Path

from benchmark.evaluate import evaluate_run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate_run(args.run)
    print(json.dumps(result["realtime"], ensure_ascii=False))


if __name__ == "__main__":
    main()
