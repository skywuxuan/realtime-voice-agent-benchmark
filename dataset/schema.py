"""Strict text-source schemas compiled into the existing runtime Scenario contract."""

import re
from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from adapters.base import SessionOptions
from benchmark.contracts import Contract, Identifier, NonNegativeInt, PositiveInt, RelativePath
from scenarios.schema import (
    AfterEvent,
    BehavioralAssertion,
    Oracle,
    Precondition,
    SessionReady,
    Termination,
    World,
)


def _safe_component(value: str, label: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise ValueError(f"{label} must be a safe path component")


class TTSProfile(Contract):
    schema_version: Literal["0.1"] = "0.1"
    profile_id: Identifier
    provider: Literal["qwen"]
    model: Identifier
    voice: Identifier
    language: Literal["Chinese"] = "Chinese"
    sample_rate_hz: Literal[16000] = 16000
    channels: Literal[1] = 1
    renderer_options: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def safe_profile(self):
        _safe_component(self.profile_id, "profile_id")
        if self.renderer_options:
            raise ValueError("provider-specific renderer options are not verified")
        return self


class TextSegment(Contract):
    type: Literal["text"]
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def trimmed(self):
        if self.text != self.text.strip():
            raise ValueError("segment text cannot have surrounding whitespace")
        return self


class SilenceSegment(Contract):
    type: Literal["silence"]
    duration_ms: PositiveInt


SpeechSegment = Annotated[TextSegment | SilenceSegment, Field(discriminator="type")]


class SpeechSource(Contract):
    text: str | None = None
    segments: tuple[SpeechSegment, ...] = ()

    @model_validator(mode="after")
    def one_representation(self):
        if (self.text is None) == (not self.segments):
            raise ValueError("speech requires exactly one of text or segments")
        if self.text is not None and (not self.text or self.text != self.text.strip()):
            raise ValueError("speech text must be nonempty and trimmed")
        if self.segments and not any(segment.type == "text" for segment in self.segments):
            raise ValueError("segmented speech needs at least one text segment")
        return self

    @property
    def reference_text(self) -> str:
        if self.text is not None:
            return self.text
        return "".join(segment.text for segment in self.segments if segment.type == "text")


class TextAction(Contract):
    action_id: Identifier
    turn_id: Identifier
    behavior: Literal["utterance", "interruption", "backchannel"] = "utterance"
    speech: SpeechSource
    trigger: SessionReady | AfterEvent
    preconditions: tuple[Precondition, ...] = ()
    intent_revision: PositiveInt | None = None


class TextScenario(Contract):
    kind: Literal["text_scenario"] = "text_scenario"
    schema_version: Literal["0.1"] = "0.1"
    scenario_id: Identifier
    scenario_version: PositiveInt = 1
    mode: Literal["half_duplex", "full_duplex"]
    category: Literal["latency", "interruption", "backchannel", "turn_taking", "pause"]
    language: str = "zh-CN"
    tags: tuple[str, ...] = ()
    seed: NonNegativeInt = 17
    world: World
    session: SessionOptions = Field(default_factory=SessionOptions)
    tts_profile: Identifier | None = None
    chunk_ms: PositiveInt = 20
    actions: tuple[TextAction, ...] = Field(min_length=1)
    oracle: Oracle | None = None
    termination: Termination = Field(default_factory=Termination)

    @model_validator(mode="after")
    def runnable_shape(self):
        _safe_component(self.scenario_id, "scenario_id")
        if self.mode == "half_duplex":
            if len(self.actions) != 1 or self.actions[0].behavior != "utterance":
                raise ValueError("half_duplex requires one ordinary user utterance")
            if not isinstance(self.actions[0].trigger, SessionReady):
                raise ValueError("half_duplex starts at session_ready")
            if self.category not in {"latency", "turn_taking", "pause"}:
                raise ValueError("half_duplex category is not supported")
        else:
            if self.category not in {"interruption", "backchannel"} or len(self.actions) != 2:
                raise ValueError(
                    "full_duplex currently requires two interruption/backchannel actions"
                )
            if not isinstance(self.actions[0].trigger, SessionReady):
                raise ValueError("full_duplex initial action starts at session_ready")
            second = self.actions[1]
            if second.behavior != self.category or not isinstance(second.trigger, AfterEvent):
                raise ValueError("full_duplex stimulus must match category and use after_event")
            if "target_response_id" not in second.trigger.bind:
                raise ValueError("full_duplex trigger must bind target_response_id")
        if self.category == "pause" and not self.actions[0].speech.segments:
            raise ValueError("pause source requires explicit speech segments")
        ids = [action.action_id for action in self.actions]
        turns = [action.turn_id for action in self.actions]
        if len(ids) != len(set(ids)) or len(turns) != len(set(turns)):
            raise ValueError("action and turn IDs must be unique")
        return self


class TextDataset(Contract):
    kind: Literal["text_dataset"] = "text_dataset"
    schema_version: Literal["0.1"] = "0.1"
    dataset_id: Identifier
    cases: tuple[TextScenario, ...] = Field(min_length=1)
    repetitions: PositiveInt = 1

    @model_validator(mode="after")
    def unique_cases(self):
        _safe_component(self.dataset_id, "dataset_id")
        if len({case.scenario_id for case in self.cases}) != len(self.cases):
            raise ValueError("scenario_id must be unique in a text dataset")
        categories = {case.category for case in self.cases}
        if len(categories) != 1:
            raise ValueError("compile each benchmark category as a separate dataset")
        return self


class TextDatasetReferences(Contract):
    kind: Literal["text_dataset_refs"] = "text_dataset_refs"
    schema_version: Literal["0.1"] = "0.1"
    dataset_id: Identifier
    cases: tuple[RelativePath, ...] = Field(min_length=1)
    repetitions: PositiveInt = 1

    @model_validator(mode="after")
    def safe_dataset(self):
        _safe_component(self.dataset_id, "dataset_id")
        if len(self.cases) != len(set(self.cases)):
            raise ValueError("duplicate text scenario reference")
        return self


class TextCorpusCase(Contract):
    id: Identifier
    text: str | None = None
    segments: tuple[SpeechSegment, ...] = ()
    initial_text: str | None = None
    initial_segments: tuple[SpeechSegment, ...] = ()
    stimulus_text: str | None = None
    stimulus_segments: tuple[SpeechSegment, ...] = ()
    tags: tuple[str, ...] = ()
    expected_new_intent: dict[str, JsonValue] = Field(default_factory=dict)
    forbidden_old_intent: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def safe_case(self):
        _safe_component(self.id, "case id")
        for value in (self.text, self.initial_text, self.stimulus_text):
            if value is not None and (not value or value != value.strip()):
                raise ValueError("corpus text must be nonempty and trimmed")
        return self


class TextCorpus(Contract):
    kind: Literal["text_corpus"] = "text_corpus"
    schema_version: Literal["0.1"] = "0.1"
    dataset_id: Identifier
    mode: Literal["half_duplex", "full_duplex"]
    category: Literal["latency", "interruption", "backchannel", "turn_taking", "pause"]
    world: World
    session: SessionOptions = Field(default_factory=SessionOptions)
    tts_profile: Identifier | None = None
    chunk_ms: PositiveInt = 20
    trigger_delay_ms: NonNegativeInt = 350
    trigger_timeout_ms: PositiveInt = 20000
    minimum_continuation_ms: PositiveInt = 800
    termination: Termination = Field(default_factory=Termination)
    cases: tuple[TextCorpusCase, ...] = Field(min_length=1)
    repetitions: PositiveInt = 1

    @model_validator(mode="after")
    def coherent_corpus(self):
        _safe_component(self.dataset_id, "dataset_id")
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate corpus case id")
        if self.mode == "half_duplex" and self.category not in {
            "latency",
            "turn_taking",
            "pause",
        }:
            raise ValueError("invalid half-duplex corpus category")
        if self.mode == "full_duplex" and self.category not in {
            "interruption",
            "backchannel",
        }:
            raise ValueError("invalid full-duplex corpus category")
        for case in self.cases:
            half_fields = case.text is not None or bool(case.segments)
            full_fields = any(
                (
                    case.initial_text is not None,
                    bool(case.initial_segments),
                    case.stimulus_text is not None,
                    bool(case.stimulus_segments),
                )
            )
            if self.mode == "half_duplex":
                if not half_fields or full_fields:
                    raise ValueError("half-duplex cases require only text or segments")
                SpeechSource(text=case.text, segments=case.segments)
            else:
                if half_fields:
                    raise ValueError("full-duplex cases use initial_* and stimulus_* fields")
                SpeechSource(text=case.initial_text, segments=case.initial_segments)
                SpeechSource(text=case.stimulus_text, segments=case.stimulus_segments)
        return self

    def expand(self) -> TextDataset:
        scenarios = []
        for case in self.cases:
            if self.mode == "half_duplex":
                speech = SpeechSource(text=case.text, segments=case.segments)
                actions = (
                    TextAction(
                        action_id="ask",
                        turn_id="t1",
                        speech=speech,
                        trigger=SessionReady(type="session_ready"),
                    ),
                )
            else:
                initial = SpeechSource(
                    text=case.initial_text,
                    segments=case.initial_segments,
                )
                stimulus = SpeechSource(
                    text=case.stimulus_text,
                    segments=case.stimulus_segments,
                )
                actions = (
                    TextAction(
                        action_id="initial",
                        turn_id="t1",
                        speech=initial,
                        trigger=SessionReady(type="session_ready"),
                    ),
                    TextAction(
                        action_id="stimulus",
                        turn_id="t2",
                        behavior=self.category,
                        speech=stimulus,
                        intent_revision=2 if self.category == "interruption" else None,
                        trigger=AfterEvent(
                            type="after_event",
                            event="assistant_playback_start",
                            where={"turn_id": "t1"},
                            occurrence=1,
                            bind={"target_response_id": "response_id"},
                            delay_ms=self.trigger_delay_ms,
                            timeout_ms=self.trigger_timeout_ms,
                        ),
                        preconditions=(
                            {
                                "type": "response_still_playing",
                                "response": "$target_response_id",
                            },
                            {
                                "type": "response_still_generating",
                                "response": "$target_response_id",
                            },
                            {
                                "type": "minimum_continuation_evidence",
                                "remaining_ms": self.minimum_continuation_ms,
                            },
                        ),
                    ),
                )
            assertion = {
                "interruption": "old_response_stops",
                "backchannel": "continues_response",
            }.get(self.category, "audio_received")
            oracle = Oracle(
                stimulus="utterance" if self.category == "latency" else self.category,
                expected_new_intent=case.expected_new_intent,
                forbidden_old_intent=case.forbidden_old_intent,
                metric_profile=f"{self.category}_text_v0_1",
                assertions=(BehavioralAssertion(type=assertion),),
            )
            scenarios.append(
                TextScenario(
                    scenario_id=case.id,
                    mode=self.mode,
                    category=self.category,
                    tags=case.tags,
                    world=self.world,
                    session=self.session,
                    tts_profile=self.tts_profile,
                    chunk_ms=self.chunk_ms,
                    actions=actions,
                    oracle=oracle,
                    termination=self.termination,
                )
            )
        return TextDataset(
            dataset_id=self.dataset_id,
            cases=tuple(scenarios),
            repetitions=self.repetitions,
        )
