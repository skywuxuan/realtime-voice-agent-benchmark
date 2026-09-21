"""Compile text-first cases to cached WAVs and runtime Scenario YAML."""

import argparse
import json
import os
from pathlib import Path

from dataset.compiler import compile_dataset, load_text_source
from dataset.schema import TTSProfile
from renderers.registry import create_renderer
from scenarios.loader import load_yaml


def compile_source_path(
    source_path: Path,
    *,
    profile_path: Path,
    asset_root: Path,
    render_root: Path = Path("datasets/rendered"),
    compiled_root: Path = Path("scenarios/compiled"),
    allow_render: bool,
) -> object:
    profile = TTSProfile.model_validate(load_yaml(profile_path))
    renderer = create_renderer(profile)
    secret = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    return compile_dataset(
        load_text_source(source_path),
        profile,
        renderer,
        asset_root=asset_root,
        render_root=render_root,
        compiled_root=compiled_root,
        allow_render=allow_render,
        secrets=(secret,) if secret else (),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument(
        "--tts-profile",
        type=Path,
        default=Path(__file__).parents[1] / "configs/tts/qwen-cherry.yaml",
    )
    parser.add_argument("--asset-root", type=Path, default=Path("."))
    parser.add_argument("--render-root", type=Path, default=Path("datasets/rendered"))
    parser.add_argument("--compiled-root", type=Path, default=Path("scenarios/compiled"))
    parser.add_argument(
        "--cache-only",
        action="store_true",
        help="compile only when every TTS text is already cached; never call the provider",
    )
    args = parser.parse_args()
    try:
        result = compile_source_path(
            args.source,
            profile_path=args.tts_profile,
            asset_root=args.asset_root,
            render_root=args.render_root,
            compiled_root=args.compiled_root,
            allow_render=not args.cache_only,
        )
    except ValueError as error:
        parser.error(str(error))
    print(
        json.dumps(
            {
                "compilation_id": result.compilation_id,
                "suite": str(result.suite_path),
                "scenarios": [str(path) for path in result.scenario_paths],
                "cache_hits": result.cache_hits,
                "cache_misses": result.cache_misses,
                "provider_calls": result.provider_calls,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
