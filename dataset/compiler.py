"""Compile text cases to immutable audio artifacts and runtime scenarios."""

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml

from benchmark.audio import AudioFormat
from benchmark.contracts import canonical_json, content_hash
from dataset.schema import (
    SilenceSegment,
    TextCorpus,
    TextDataset,
    TextDatasetReferences,
    TextScenario,
    TTSProfile,
)
from events.redaction import Redactor
from events.replay import artifact_path, file_hash
from renderers.base import TTSRenderer
from scenarios.loader import load_suite, load_yaml
from scenarios.schema import (
    AudioAsset,
    BehavioralAssertion,
    BoundaryAnnotation,
    InputRegion,
    Oracle,
    PlayAudio,
    Scenario,
    ScenarioAudio,
    Suite,
)
from simulator.audio import estimate_speech_bounds, read_wav_bytes, wav_bytes

COMPILER_VERSION = "text-dataset-compiler-0.1"
TEXT_RENDER_RECIPE = "tts_pcm16_energy_rms_v1"
COMPOSITE_RENDER_RECIPE = "trim_speech_then_insert_silence_v1"


def compiler_fingerprint() -> dict[str, str]:
    root = Path(__file__).parents[1]
    files = (
        "dataset/compiler.py",
        "dataset/schema.py",
        "renderers/base.py",
        "scenarios/schema.py",
        "simulator/audio.py",
    )
    return {path: file_hash(root / path) for path in files}


class MissingRenderedAudio(ValueError):
    pass


@dataclass(frozen=True)
class CachedAudio:
    render_id: str
    wav_path: Path
    metadata_path: Path
    pcm: bytes
    speech_bounds_samples: tuple[int, int]
    boundary_annotation: BoundaryAnnotation
    cache_hit: bool


@dataclass(frozen=True)
class CompilationResult:
    suite_path: Path
    scenario_paths: tuple[Path, ...]
    manifest_path: Path
    compilation_id: str
    cache_hits: int
    cache_misses: int
    provider_calls: int


def _write_immutable(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"immutable artifact differs: {path}")
        return
    with NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError(f"immutable artifact differs: {path}")
        else:
            temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class RenderCache:
    def __init__(
        self,
        root: Path,
        profile: TTSProfile,
        renderer: TTSRenderer,
        *,
        allow_render: bool,
        secrets: tuple[str, ...] = (),
    ):
        self.root = root
        self.profile = profile
        self.renderer = renderer
        self.allow_render = allow_render
        self.redactor = Redactor(secrets)
        self.cache_hits = 0
        self.cache_misses = 0
        self.provider_calls = 0
        self.fingerprint = renderer.fingerprint()
        self.compiler_fingerprint = compiler_fingerprint()
        self.profile_hash = content_hash(
            {"profile": profile.model_dump(mode="json"), "renderer": self.fingerprint}
        )

    def _paths(self, render_id: str) -> tuple[Path, Path]:
        directory = self.root / self.profile.profile_id
        return directory / f"{render_id}.wav", directory / f"{render_id}.json"

    def _load(self, identity: dict, render_id: str) -> CachedAudio | None:
        wav_path, metadata_path = self._paths(render_id)
        if wav_path.exists() != metadata_path.exists():
            raise ValueError("render cache has an incomplete artifact pair")
        if not wav_path.exists():
            return None
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("identity") != identity or metadata.get("render_id") != render_id:
            raise ValueError("render cache identity mismatch")
        if file_hash(wav_path) != metadata.get("wav_sha256"):
            raise ValueError("render cache WAV hash mismatch")
        format = AudioFormat.model_validate(metadata["format"])
        if format.sample_rate_hz != self.profile.sample_rate_hz or format.channels != 1:
            raise ValueError("render cache format differs from profile")
        pcm = read_wav_bytes(wav_path.read_bytes(), format)
        if len(pcm) // format.bytes_per_sample_frame != metadata["sample_count"]:
            raise ValueError("render cache sample count mismatch")
        return CachedAudio(
            render_id,
            wav_path,
            metadata_path,
            pcm,
            tuple(metadata["speech_bounds_samples"]),
            BoundaryAnnotation.model_validate(metadata["boundary_annotation"]),
            True,
        )

    def _load_legacy_text(self, *, text: str) -> CachedAudio | None:
        """Reuse v0.1 assets whose identity included an overly broad compiler hash."""
        directory = self.root / self.profile.profile_id
        if not directory.exists():
            return None
        for metadata_path in sorted(directory.glob("*.json")):
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            identity = metadata.get("identity", {})
            if (
                identity.get("kind") == "tts_text"
                and identity.get("text") == text
                and identity.get("profile_hash") == self.profile_hash
                and "compiler_fingerprint" in identity
            ):
                return self._load(identity, metadata["render_id"])
        return None

    def _store(
        self,
        identity: dict,
        pcm: bytes,
        bounds: tuple[int, int],
        annotation: BoundaryAnnotation,
        provider_metadata: dict,
    ) -> CachedAudio:
        render_id = content_hash(identity)
        existing = self._load(identity, render_id)
        if existing:
            return existing
        format = AudioFormat(sample_rate_hz=self.profile.sample_rate_hz)
        if not pcm or len(pcm) % format.bytes_per_sample_frame:
            raise ValueError("renderer produced invalid PCM")
        if not 0 <= bounds[0] < bounds[1] <= len(pcm) // format.bytes_per_sample_frame:
            raise ValueError("renderer produced invalid speech bounds")
        wav = wav_bytes(pcm, format)
        clean_metadata, _ = self.redactor.clean(provider_metadata)
        metadata = {
            "schema_version": "0.1",
            "render_id": render_id,
            "identity": identity,
            "profile": self.profile.model_dump(mode="json"),
            "renderer_fingerprint": self.fingerprint,
            "format": format.model_dump(mode="json"),
            "sample_count": len(pcm) // format.bytes_per_sample_frame,
            "speech_bounds_samples": list(bounds),
            "boundary_annotation": annotation.model_dump(mode="json"),
            "wav_sha256": content_hash_bytes(wav),
            "provider_metadata": clean_metadata,
            "producer": {
                "compiler_version": COMPILER_VERSION,
                "compiler_fingerprint": self.compiler_fingerprint,
            },
        }
        wav_path, metadata_path = self._paths(render_id)
        _write_immutable(wav_path, wav)
        _write_immutable(metadata_path, (canonical_json(metadata) + "\n").encode("utf-8"))
        return CachedAudio(
            render_id,
            wav_path,
            metadata_path,
            pcm,
            bounds,
            annotation,
            False,
        )

    def render_text(self, text: str) -> CachedAudio:
        identity = {
            "kind": "tts_text",
            "text": text,
            "profile_hash": self.profile_hash,
            "recipe": TEXT_RENDER_RECIPE,
        }
        render_id = content_hash(identity)
        existing = self._load(identity, render_id)
        if existing is None:
            existing = self._load_legacy_text(text=text)
        if existing:
            self.cache_hits += 1
            return existing
        if not self.allow_render:
            raise MissingRenderedAudio(
                f"missing rendered audio {render_id}; rerun with --render-missing"
            )
        self.cache_misses += 1
        self.provider_calls += 1
        started = time.monotonic_ns()
        rendered = self.renderer.synthesize(text)
        if rendered.format != AudioFormat(sample_rate_hz=self.profile.sample_rate_hz):
            raise ValueError("renderer output format differs from TTS profile")
        bounds, parameters = estimate_speech_bounds(rendered.pcm, rendered.format.sample_rate_hz)
        annotation = BoundaryAnnotation(
            method="energy_rms_v1",
            status="automatic",
            resolution_ms=20,
            parameters=parameters,
        )
        return self._store(
            identity,
            rendered.pcm,
            bounds,
            annotation,
            {
                **rendered.provider_metadata,
                "render_duration_ms": (time.monotonic_ns() - started) / 1e6,
            },
        )

    def render_composite(self, segments) -> tuple[CachedAudio, tuple[InputRegion, ...]]:
        children = []
        pcm = bytearray()
        regions = []
        rate = self.profile.sample_rate_hz
        for index, segment in enumerate(segments):
            if isinstance(segment, SilenceSegment):
                start = len(pcm) // 2
                samples = rate * segment.duration_ms // 1000
                if samples * 1000 != rate * segment.duration_ms:
                    raise ValueError("silence duration does not map to whole samples")
                pcm.extend(b"\0\0" * samples)
                regions.append(
                    InputRegion(
                        region_id=f"pause_{len(regions) + 1}",
                        kind="pause",
                        bounds_samples=(start, start + samples),
                    )
                )
                children.append({"type": "silence", "duration_ms": segment.duration_ms})
                continue
            child = self.render_text(segment.text)
            start, end = child.speech_bounds_samples
            pcm.extend(child.pcm[start * 2 : end * 2])
            children.append(
                {
                    "type": "text",
                    "text": segment.text,
                    "render_id": child.render_id,
                    "trimmed_bounds_samples": [start, end],
                }
            )
        samples = len(pcm) // 2
        identity = {
            "kind": "tts_segments",
            "children": children,
            "profile_hash": self.profile_hash,
            "recipe": COMPOSITE_RENDER_RECIPE,
        }
        existing = self._load(identity, content_hash(identity))
        if existing:
            self.cache_hits += 1
            return existing, tuple(regions)
        self.cache_misses += 1
        annotation = BoundaryAnnotation(
            method="tts_segments_v1",
            status="automatic",
            resolution_ms=1000 / rate,
            parameters={
                "recipe": COMPOSITE_RENDER_RECIPE,
                "segment_count": len(segments),
            },
        )
        artifact = self._store(identity, bytes(pcm), (0, samples), annotation, {})
        return artifact, tuple(regions)


def content_hash_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def load_text_source(path: Path) -> TextDataset:
    if path.suffix == ".jsonl":
        cases = []
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                cases.append(TextScenario.model_validate_json(line))
            except ValueError as error:
                raise ValueError(f"{path.name}:{number}: invalid text scenario") from error
        return TextDataset(dataset_id=path.stem, cases=tuple(cases))
    data = load_yaml(path)
    kind = data.get("kind")
    if kind == "text_scenario":
        case = TextScenario.model_validate(data)
        return TextDataset(dataset_id=case.scenario_id, cases=(case,))
    if kind == "text_dataset":
        return TextDataset.model_validate(data)
    if kind == "text_dataset_refs":
        references = TextDatasetReferences.model_validate(data)
        cases = tuple(
            TextScenario.model_validate(load_yaml(artifact_path(path.parent, case)))
            for case in references.cases
        )
        return TextDataset(
            dataset_id=references.dataset_id,
            cases=cases,
            repetitions=references.repetitions,
        )
    if kind == "text_corpus":
        return TextCorpus.model_validate(data).expand()
    raise ValueError("not a text-source scenario or dataset")


def is_text_source(path: Path) -> bool:
    if path.suffix == ".jsonl":
        return True
    try:
        return load_yaml(path).get("kind") in {
            "text_scenario",
            "text_dataset",
            "text_dataset_refs",
            "text_corpus",
        }
    except ValueError:
        return False


def _default_oracle(case: TextScenario) -> Oracle:
    stimulus = "utterance" if case.category == "latency" else case.category
    assertion = {
        "interruption": "old_response_stops",
        "backchannel": "continues_response",
    }.get(case.category, "audio_received")
    return Oracle(
        stimulus=stimulus,
        metric_profile=f"{case.category}_text_v0_1",
        assertions=(BehavioralAssertion(type=assertion),),
    )


def compile_dataset(
    source: TextDataset,
    profile: TTSProfile,
    renderer: TTSRenderer,
    *,
    asset_root: Path,
    render_root: Path,
    compiled_root: Path,
    allow_render: bool,
    secrets: tuple[str, ...] = (),
) -> CompilationResult:
    asset_root = asset_root.resolve()
    cache_root = artifact_path(asset_root, render_root.as_posix())
    compiled_base = artifact_path(asset_root, compiled_root.as_posix())
    cache = RenderCache(
        cache_root,
        profile,
        renderer,
        allow_render=allow_render,
        secrets=secrets,
    )
    source_data = source.model_dump(mode="json")
    compilation_id = content_hash(
        {
            "source": source_data,
            "profile_hash": cache.profile_hash,
            "compiler_version": COMPILER_VERSION,
            "compiler_fingerprint": cache.compiler_fingerprint,
        }
    )[:20]
    directory = compiled_base / source.dataset_id / compilation_id
    scenarios = []
    paths = []
    for case in source.cases:
        if case.tts_profile and case.tts_profile != profile.profile_id:
            raise ValueError("text scenario requests a different TTS profile")
        assets = {}
        actions = []
        for action in case.actions:
            if action.speech.text is not None:
                rendered = cache.render_text(action.speech.text)
                regions = ()
            else:
                rendered, regions = cache.render_composite(action.speech.segments)
            relative_wav = rendered.wav_path.relative_to(asset_root).as_posix()
            relative_metadata = rendered.metadata_path.relative_to(asset_root).as_posix()
            assets[action.action_id] = AudioAsset(
                path=relative_wav,
                sha256=file_hash(rendered.wav_path),
                reference_text=action.speech.reference_text,
                speech_bounds_samples=rendered.speech_bounds_samples,
                sample_rate_hz=profile.sample_rate_hz,
                provenance={
                    "kind": "frozen_tts",
                    "speaker_id": profile.voice,
                    "generator": profile.model,
                },
                boundary_annotation=rendered.boundary_annotation,
                regions=regions,
                derivation={
                    "renderer_profile_id": profile.profile_id,
                    "renderer_profile_sha256": cache.profile_hash,
                    "render_id": rendered.render_id,
                    "metadata_path": relative_metadata,
                    "compiler_version": COMPILER_VERSION,
                    "compiler_fingerprint": cache.compiler_fingerprint,
                },
            )
            actions.append(
                PlayAudio(
                    action_id=action.action_id,
                    type="play_audio",
                    asset=action.action_id,
                    turn_id=action.turn_id,
                    stimulus=action.behavior,
                    intent_revision=action.intent_revision,
                    trigger=action.trigger,
                    preconditions=action.preconditions,
                )
            )
        scenario = Scenario(
            schema_version="0.1",
            scenario_id=case.scenario_id,
            scenario_version=case.scenario_version,
            suite="realtime",
            category=case.category,
            language=case.language,
            tags=tuple(dict.fromkeys((*case.tags, "text_source", "tts_rendered"))),
            seed=case.seed,
            world=case.world,
            capabilities_required=(
                "audio_input",
                "audio_output",
                "streaming_input",
                "streaming_output",
                "server_vad",
            ),
            session=case.session,
            audio=ScenarioAudio(chunk_ms=case.chunk_ms, assets=assets),
            actions=tuple(actions),
            oracle=case.oracle or _default_oracle(case),
            termination=case.termination,
        )
        scenarios.append(scenario)
        path = directory / f"{scenario.scenario_id}.yaml"
        data = yaml.safe_dump(
            scenario.model_dump(mode="json"), allow_unicode=True, sort_keys=True
        ).encode("utf-8")
        _write_immutable(path, data)
        paths.append(path)
    suite = Suite(
        schema_version="0.1",
        suite_id=f"{source.dataset_id}_{compilation_id}",
        cases=tuple(path.name for path in paths),
        repetitions=source.repetitions,
    )
    suite_path = directory / "suite.yaml"
    _write_immutable(
        suite_path,
        yaml.safe_dump(suite.model_dump(mode="json"), allow_unicode=True, sort_keys=True).encode(
            "utf-8"
        ),
    )
    source_path = directory / "source.json"
    _write_immutable(source_path, (canonical_json(source_data) + "\n").encode("utf-8"))
    manifest = {
        "schema_version": "0.1",
        "kind": "compiled_text_dataset",
        "compiler_version": COMPILER_VERSION,
        "compiler_fingerprint": cache.compiler_fingerprint,
        "compilation_id": compilation_id,
        "dataset_id": source.dataset_id,
        "source_sha256": content_hash(source_data),
        "tts_profile": profile.model_dump(mode="json"),
        "renderer_fingerprint": cache.fingerprint,
        "renderer_profile_sha256": cache.profile_hash,
        "suite": suite_path.name,
        "files": {
            path.relative_to(directory).as_posix(): file_hash(path)
            for path in (*paths, suite_path, source_path)
        },
    }
    manifest_path = directory / "manifest.json"
    _write_immutable(manifest_path, (canonical_json(manifest) + "\n").encode("utf-8"))
    load_suite(suite_path, asset_root=asset_root)
    return CompilationResult(
        suite_path,
        tuple(paths),
        manifest_path,
        compilation_id,
        cache.cache_hits,
        cache.cache_misses,
        cache.provider_calls,
    )
