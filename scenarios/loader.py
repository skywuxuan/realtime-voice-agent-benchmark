"""Load canonical YAML and verify frozen WAV assets without network access."""

import wave
from pathlib import Path

import yaml

from events.replay import artifact_path, file_hash
from scenarios.schema import Scenario, Suite


class ScenarioError(ValueError):
    pass


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ScenarioError(f"duplicate YAML key at line {key_node.start_mark.line + 1}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_yaml(path: str | Path) -> dict:
    try:
        data = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except (OSError, yaml.YAMLError, TypeError) as error:
        raise ScenarioError("cannot read a valid scenario YAML mapping") from error
    if not isinstance(data, dict):
        raise ScenarioError("YAML document must be a mapping")
    return data


def validate_assets(scenario: Scenario, asset_root: str | Path) -> None:
    for asset_id, asset in scenario.audio.assets.items():
        path = artifact_path(Path(asset_root), asset.path)
        try:
            if file_hash(path) != asset.sha256:
                raise ScenarioError(f"asset {asset_id}: SHA-256 mismatch")
            with wave.open(str(path), "rb") as audio:
                if (
                    audio.getnchannels(),
                    audio.getsampwidth(),
                    audio.getframerate(),
                    audio.getcomptype(),
                ) != (
                    scenario.audio.channels,
                    2,
                    asset.sample_rate_hz,
                    "NONE",
                ):
                    raise ScenarioError(
                        f"asset {asset_id}: expected mono PCM16 WAV at declared rate"
                    )
                if asset.speech_bounds_samples[1] > audio.getnframes():
                    raise ScenarioError(f"asset {asset_id}: speech boundary extends beyond WAV")
                if any(region.bounds_samples[1] > audio.getnframes() for region in asset.regions):
                    raise ScenarioError(f"asset {asset_id}: region boundary extends beyond WAV")
                # Header frame count alone cannot detect a truncated WAV body.
                pcm = audio.readframes(audio.getnframes())
                if len(pcm) != audio.getnframes() * audio.getnchannels() * audio.getsampwidth():
                    raise ScenarioError(f"asset {asset_id}: truncated WAV samples")
        except (OSError, EOFError, wave.Error) as error:
            raise ScenarioError(f"asset {asset_id}: cannot read WAV") from error


def load_scenario(path: str | Path, *, asset_root: str | Path | None = None) -> Scenario:
    scenario = Scenario.model_validate(load_yaml(path))
    if asset_root is not None:
        validate_assets(scenario, asset_root)
    return scenario


def load_suite(
    path: str | Path, *, asset_root: str | Path | None = None
) -> tuple[Suite, tuple[Scenario, ...]]:
    path = Path(path)
    suite = Suite.model_validate(load_yaml(path))
    scenarios = tuple(
        load_scenario(artifact_path(path.parent, case), asset_root=asset_root)
        for case in suite.cases
    )
    ids = [scenario.scenario_id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise ScenarioError("scenario_id must be unique across a suite")
    return suite, scenarios
