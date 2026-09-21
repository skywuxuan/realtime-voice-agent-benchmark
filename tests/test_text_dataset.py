import json
from pathlib import Path

import pytest
import yaml

from benchmark.audio import AudioFormat
from dataset.compiler import MissingRenderedAudio, compile_dataset, load_text_source
from dataset.schema import TextDataset, TextScenario, TTSProfile
from events.replay import file_hash
from renderers.base import RenderedText, TTSRenderer
from scenarios.loader import load_scenario, load_suite
from simulator.audio import read_wav_bytes


class FixtureRenderer(TTSRenderer):
    def __init__(self, *, secret="unit-render-secret"):
        self.calls = []
        self.secret = secret

    def fingerprint(self):
        return {
            "provider": "fixture",
            "renderer_version": "fixture-1",
            "decoder": "none",
        }

    def synthesize(self, text):
        self.calls.append(text)
        value = 100 + len(self.calls)
        return RenderedText(
            value.to_bytes(2, "little", signed=True) * 640,
            AudioFormat(sample_rate_hz=16000),
            {"request_id": f"request_{len(self.calls)}", "secret": self.secret},
        )


def profile():
    return TTSProfile(
        profile_id="fixture_16k_v1",
        provider="qwen",
        model="fixture-tts",
        voice="fixture",
    )


def text_case(*, mode="half_duplex", category="latency", speech=None):
    first = {
        "action_id": "ask",
        "turn_id": "t1",
        "behavior": "utterance",
        "speech": speech or {"text": "请介绍北京。"},
        "trigger": {"type": "session_ready"},
    }
    actions = [first]
    if mode == "full_duplex":
        actions.append(
            {
                "action_id": "correct",
                "turn_id": "t2",
                "behavior": category,
                "speech": {"text": "等等，改成上海。"},
                "intent_revision": 2,
                "trigger": {
                    "type": "after_event",
                    "event": "assistant_playback_start",
                    "where": {"turn_id": "t1"},
                    "occurrence": 1,
                    "bind": {"target_response_id": "response_id"},
                    "delay_ms": 350,
                    "timeout_ms": 10000,
                },
                "preconditions": [
                    {"type": "response_still_playing", "response": "$target_response_id"}
                ],
            }
        )
    return TextScenario.model_validate(
        {
            "scenario_id": f"text_{category}_001",
            "mode": mode,
            "category": category,
            "world": {"now": "2026-09-21T10:00:00+08:00", "timezone": "Asia/Shanghai"},
            "session": {
                "system_prompt": "fixture",
                "turn_mode": "server_vad",
                "control_profile": "native_server",
            },
            "tts_profile": "fixture_16k_v1",
            "actions": actions,
        }
    )


def compile_fixture(tmp_path, dataset, renderer, *, allow_render=True):
    return compile_dataset(
        dataset,
        profile(),
        renderer,
        asset_root=tmp_path,
        render_root=Path("datasets/rendered"),
        compiled_root=Path("scenarios/compiled"),
        allow_render=allow_render,
        secrets=(renderer.secret,),
    )


def test_half_duplex_text_is_rendered_once_and_cache_is_immutable(tmp_path):
    dataset = TextDataset(dataset_id="half_fixture", cases=(text_case(),))
    renderer = FixtureRenderer()
    first = compile_fixture(tmp_path, dataset, renderer)
    assert renderer.calls == ["请介绍北京。"]
    assert (first.cache_hits, first.cache_misses) == (0, 1)
    assert first.provider_calls == 1
    suite, cases = load_suite(first.suite_path, asset_root=tmp_path)
    assert len(cases) == 1 and suite.repetitions == 1
    scenario = cases[0]
    asset = scenario.audio.assets["ask"]
    assert asset.derivation["renderer_profile_id"] == "fixture_16k_v1"
    assert file_hash(tmp_path / asset.path) == asset.sha256
    assert "unit-render-secret" not in (tmp_path / asset.derivation["metadata_path"]).read_text()
    assert "secret oracle" not in scenario.model_session_options().model_dump_json()

    cached_renderer = FixtureRenderer()
    second = compile_fixture(tmp_path, dataset, cached_renderer, allow_render=False)
    assert cached_renderer.calls == []
    assert second.compilation_id == first.compilation_id
    assert second.cache_hits == 1 and second.cache_misses == 0
    assert second.provider_calls == 0
    assert first.manifest_path.read_bytes() == second.manifest_path.read_bytes()


def test_cache_only_fails_before_creating_a_compiled_dataset(tmp_path):
    dataset = TextDataset(dataset_id="missing_fixture", cases=(text_case(),))
    with pytest.raises(MissingRenderedAudio, match="render-missing"):
        compile_fixture(tmp_path, dataset, FixtureRenderer(), allow_render=False)
    assert not (tmp_path / "scenarios/compiled").exists()


def test_segmented_pause_has_exact_samples_and_reuses_text_cache(tmp_path):
    speech = {
        "segments": [
            {"type": "text", "text": "帮我查一下"},
            {"type": "silence", "duration_ms": 800},
            {"type": "text", "text": "明天的高铁"},
        ]
    }
    case = text_case(category="pause", speech=speech)
    result = compile_fixture(
        tmp_path,
        TextDataset(dataset_id="pause_fixture", cases=(case,)),
        FixtureRenderer(),
    )
    scenario = load_scenario(result.scenario_paths[0], asset_root=tmp_path)
    asset = scenario.audio.assets["ask"]
    pause = asset.regions[0]
    assert pause.kind == "pause"
    assert pause.bounds_samples[1] - pause.bounds_samples[0] == 12800
    pcm = read_wav_bytes((tmp_path / asset.path).read_bytes(), AudioFormat(sample_rate_hz=16000))
    assert len(pcm) // 2 == 640 + 12800 + 640
    assert pcm[pause.bounds_samples[0] * 2 : pause.bounds_samples[1] * 2] == b"\0\0" * 12800
    assert asset.boundary_annotation.method == "tts_segments_v1"
    assert result.cache_misses == 3  # two provider results plus the local composite
    assert result.provider_calls == 2


@pytest.mark.parametrize("category", ["interruption", "backchannel"])
def test_full_duplex_text_compiles_event_triggered_audio(category, tmp_path):
    case = text_case(mode="full_duplex", category=category)
    result = compile_fixture(
        tmp_path,
        TextDataset(dataset_id=f"full_{category}", cases=(case,)),
        FixtureRenderer(),
    )
    scenario = load_scenario(result.scenario_paths[0], asset_root=tmp_path)
    assert len(scenario.actions) == 2
    assert scenario.actions[1].stimulus == category
    assert scenario.actions[1].trigger.event == "assistant_playback_start"
    assert scenario.actions[1].trigger.delay_ms == 350
    assert scenario.oracle.stimulus == category
    assert len(scenario.audio.assets) == 2


def test_text_source_yaml_references_and_jsonl_are_strict(tmp_path):
    first = text_case().model_dump(mode="json")
    second = (
        text_case().model_copy(update={"scenario_id": "text_latency_002"}).model_dump(mode="json")
    )
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(first, allow_unicode=True))
    (tmp_path / "b.yaml").write_text(yaml.safe_dump(second, allow_unicode=True))
    refs = {
        "kind": "text_dataset_refs",
        "schema_version": "0.1",
        "dataset_id": "refs_fixture",
        "cases": ["a.yaml", "b.yaml"],
    }
    refs_path = tmp_path / "refs.yaml"
    refs_path.write_text(yaml.safe_dump(refs, allow_unicode=True))
    assert len(load_text_source(refs_path).cases) == 2

    jsonl = tmp_path / "lines.jsonl"
    jsonl.write_text(
        "\n".join((json.dumps(first, ensure_ascii=False), json.dumps(second, ensure_ascii=False)))
    )
    assert len(load_text_source(jsonl).cases) == 2
    jsonl.write_text(json.dumps(first) + "\nnot-json\n")
    with pytest.raises(ValueError, match="lines.jsonl:2"):
        load_text_source(jsonl)


def test_compact_half_duplex_corpus_expands_and_compiles(tmp_path):
    source = tmp_path / "half.yaml"
    source.write_text(
        yaml.safe_dump(
            {
                "kind": "text_corpus",
                "schema_version": "0.1",
                "dataset_id": "compact_half",
                "mode": "half_duplex",
                "category": "latency",
                "world": {
                    "now": "2026-09-21T10:00:00+08:00",
                    "timezone": "Asia/Shanghai",
                },
                "tts_profile": "fixture_16k_v1",
                "cases": [
                    {"id": "compact_half_001", "text": "你好。"},
                    {"id": "compact_half_002", "text": "一加一等于几？"},
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    dataset = load_text_source(source)
    assert [case.actions[0].speech.reference_text for case in dataset.cases] == [
        "你好。",
        "一加一等于几？",
    ]
    result = compile_fixture(tmp_path, dataset, FixtureRenderer())
    suite, scenarios = load_suite(result.suite_path, asset_root=tmp_path)
    assert suite.suite_id.startswith("compact_half_")
    assert len(scenarios) == 2
    assert all(case.oracle.assertions[0].type == "audio_received" for case in scenarios)


def test_compact_full_duplex_corpus_expands_event_trigger(tmp_path):
    source = tmp_path / "full.yaml"
    source.write_text(
        yaml.safe_dump(
            {
                "kind": "text_corpus",
                "schema_version": "0.1",
                "dataset_id": "compact_full",
                "mode": "full_duplex",
                "category": "interruption",
                "world": {
                    "now": "2026-09-21T10:00:00+08:00",
                    "timezone": "Asia/Shanghai",
                },
                "tts_profile": "fixture_16k_v1",
                "trigger_delay_ms": 425,
                "cases": [
                    {
                        "id": "compact_full_001",
                        "initial_text": "请详细介绍北京。",
                        "stimulus_text": "等等，改成上海。",
                        "expected_new_intent": {"city": "上海"},
                        "forbidden_old_intent": {"city": "北京"},
                    }
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    dataset = load_text_source(source)
    result = compile_fixture(tmp_path, dataset, FixtureRenderer())
    scenario = load_scenario(result.scenario_paths[0], asset_root=tmp_path)
    stimulus = scenario.actions[1]
    assert stimulus.stimulus == "interruption"
    assert stimulus.trigger.delay_ms == 425
    assert {item.type for item in stimulus.preconditions} == {
        "response_still_playing",
        "response_still_generating",
        "minimum_continuation_evidence",
    }
    assert scenario.oracle.expected_new_intent == {"city": "上海"}
    assert scenario.oracle.forbidden_old_intent == {"city": "北京"}


@pytest.mark.parametrize(
    ("mode", "case"),
    [
        ("half_duplex", {"id": "bad_half", "initial_text": "你好", "stimulus_text": "等等"}),
        ("full_duplex", {"id": "bad_full", "text": "你好"}),
    ],
)
def test_compact_corpus_rejects_fields_for_the_other_mode(tmp_path, mode, case):
    category = "latency" if mode == "half_duplex" else "interruption"
    source = tmp_path / "bad.yaml"
    source.write_text(
        yaml.safe_dump(
            {
                "kind": "text_corpus",
                "schema_version": "0.1",
                "dataset_id": "bad_corpus",
                "mode": mode,
                "category": category,
                "world": {
                    "now": "2026-09-21T10:00:00+08:00",
                    "timezone": "Asia/Shanghai",
                },
                "cases": [case],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_text_source(source)


@pytest.mark.parametrize(
    "change",
    ["half_multiple", "full_single", "pause_text", "unsafe_id", "wrong_profile"],
)
def test_text_source_rejects_non_runnable_shapes(change):
    data = text_case().model_dump(mode="json")
    if change == "half_multiple":
        data["actions"].append(data["actions"][0] | {"action_id": "again", "turn_id": "t2"})
    elif change == "full_single":
        data["mode"] = "full_duplex"
        data["category"] = "interruption"
    elif change == "pause_text":
        data["category"] = "pause"
    elif change == "unsafe_id":
        data["scenario_id"] = "../escape"
    else:
        case = TextScenario.model_validate(data).model_copy(update={"tts_profile": "other"})
        dataset = TextDataset(dataset_id="profile_fixture", cases=(case,))
        with pytest.raises(ValueError, match="different TTS profile"):
            compile_dataset(
                dataset,
                profile(),
                FixtureRenderer(),
                asset_root=Path("/tmp"),
                render_root=Path("profile-fixture-rendered"),
                compiled_root=Path("profile-fixture-compiled"),
                allow_render=False,
            )
        return
    with pytest.raises(ValueError):
        TextScenario.model_validate(data)
