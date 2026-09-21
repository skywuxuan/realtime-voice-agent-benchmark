"""Strict MVP realtime scenario schema. Stimuli/oracles are never model input."""

from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from adapters.base import CapabilityName, SessionOptions
from benchmark.contracts import (
    Contract,
    Identifier,
    NonNegativeInt,
    PositiveInt,
    RelativePath,
    Sha256,
    content_hash,
)
from events.schema import EventType


class World(Contract):
    now: AwareDatetime
    timezone: str = "Asia/Shanghai"

    @model_validator(mode="after")
    def valid_timezone(self) -> "World":
        try:
            zone = ZoneInfo(self.timezone)
        except (KeyError, ValueError) as error:
            raise ValueError("unknown world timezone") from error
        if self.now.utcoffset() != self.now.astimezone(zone).utcoffset():
            raise ValueError("world.now offset must agree with world.timezone")
        return self


class Provenance(Contract):
    kind: Literal["human", "frozen_tts", "synthetic_fixture"]
    speaker_id: Identifier
    license: str | None = None
    generator: str | None = None


class BoundaryAnnotation(Contract):
    method: Literal[
        "unverified", "manual", "forced_alignment", "energy_rms_v1", "fixture", "tts_segments_v1"
    ] = "unverified"
    status: Literal["unverified", "human_reviewed", "automatic", "synthetic"] = "unverified"
    resolution_ms: Annotated[float, Field(ge=0)] | None = None
    parameters: dict[str, JsonValue] = Field(default_factory=dict)


class InputRegion(Contract):
    region_id: Identifier
    kind: Literal["pause", "interferer"]
    bounds_samples: tuple[NonNegativeInt, PositiveInt]
    subtype: Literal["ambient_speech", "side_conversation", "simultaneous_speech"] | None = None

    @model_validator(mode="after")
    def valid_region(self):
        if self.bounds_samples[0] >= self.bounds_samples[1]:
            raise ValueError("input region must be nonempty")
        if (self.kind == "interferer") != (self.subtype is not None):
            raise ValueError("only interference regions require subtype")
        return self


class AudioAsset(Contract):
    path: RelativePath
    sha256: Sha256
    reference_text: str
    speech_bounds_samples: tuple[NonNegativeInt, PositiveInt]
    sample_rate_hz: PositiveInt
    provenance: Provenance
    boundary_annotation: BoundaryAnnotation = Field(default_factory=BoundaryAnnotation)
    regions: tuple[InputRegion, ...] = ()
    derivation: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_speech_bounds(self) -> "AudioAsset":
        if self.speech_bounds_samples[0] >= self.speech_bounds_samples[1]:
            raise ValueError("speech bounds must define a nonempty interval")
        if len({r.region_id for r in self.regions}) != len(self.regions):
            raise ValueError("duplicate region_id")
        pauses = sorted(r.bounds_samples for r in self.regions if r.kind == "pause")
        for start, end in pauses:
            if not self.speech_bounds_samples[0] < start < end < self.speech_bounds_samples[1]:
                raise ValueError("pause must lie strictly inside one user utterance")
        if any(left[1] > right[0] for left, right in zip(pauses, pauses[1:])):
            raise ValueError("pause regions cannot overlap")
        return self


class ScenarioAudio(Contract):
    input_encoding: Literal["pcm_s16le"] = "pcm_s16le"
    channels: Literal[1] = 1
    chunk_ms: PositiveInt = 20
    assets: Annotated[dict[Identifier, AudioAsset], Field(min_length=1)]

    @model_validator(mode="after")
    def whole_chunks(self) -> "ScenarioAudio":
        if any(asset.sample_rate_hz * self.chunk_ms % 1000 for asset in self.assets.values()):
            raise ValueError("chunk_ms must represent whole sample frames for every asset")
        return self


class SessionReady(Contract):
    type: Literal["session_ready"]


class AtOffset(Contract):
    type: Literal["at_offset"]
    offset_ms: NonNegativeInt


class AfterEvent(Contract):
    type: Literal["after_event"]
    event: EventType
    where: dict[Literal["turn_id", "response_id", "event_id", "action_id"], str]
    occurrence: PositiveInt
    bind: dict[Literal["target_response_id"], Literal["response_id"]] = Field(default_factory=dict)
    delay_ms: NonNegativeInt = 0
    timeout_ms: PositiveInt


Trigger = Annotated[SessionReady | AtOffset | AfterEvent, Field(discriminator="type")]


class ResponsePrecondition(Contract):
    type: Literal["response_still_playing", "response_still_generating"]
    response: Literal["$target_response_id"]


class ContinuationPrecondition(Contract):
    type: Literal["minimum_continuation_evidence"]
    remaining_ms: PositiveInt


Precondition = Annotated[
    ResponsePrecondition | ContinuationPrecondition, Field(discriminator="type")
]


class PlayAudio(Contract):
    action_id: Identifier
    type: Literal["play_audio"]
    asset: Identifier
    turn_id: Identifier
    stimulus: Literal["utterance", "interruption", "backchannel"] = "utterance"
    intent_revision: PositiveInt | None = None
    trigger: Trigger
    preconditions: tuple[Precondition, ...] = ()

    @model_validator(mode="after")
    def bound_preconditions(self) -> "PlayAudio":
        if self.preconditions or self.stimulus != "utterance":
            if (
                not isinstance(self.trigger, AfterEvent)
                or "target_response_id" not in self.trigger.bind
            ):
                raise ValueError(
                    "response preconditions/stimuli require a bound target_response_id"
                )
        return self


class BehavioralAssertion(Contract):
    type: Literal["old_response_stops", "audio_received", "continues_response"]


class AnswerTargetsCity(Contract):
    type: Literal["answer_targets_city"]
    city: str


Assertion = Annotated[BehavioralAssertion | AnswerTargetsCity, Field(discriminator="type")]


class Oracle(Contract):
    hidden_from_model: Literal[True] = True
    stimulus: Literal["utterance", "interruption", "backchannel", "turn_taking", "pause", "overlap"]
    expected_new_intent: dict[str, JsonValue] = Field(default_factory=dict)
    forbidden_old_intent: dict[str, JsonValue] = Field(default_factory=dict)
    metric_profile: Identifier
    assertions: Annotated[tuple[Assertion, ...], Field(min_length=1)]


class Termination(Contract):
    max_case_duration_ms: PositiveInt = 60000
    response_timeout_ms: PositiveInt = 15000
    post_stimulus_observation_ms: PositiveInt = 5000
    drain_timeout_ms: PositiveInt = 5000

    @model_validator(mode="after")
    def timeouts_fit(self) -> "Termination":
        if (
            max(self.response_timeout_ms, self.post_stimulus_observation_ms, self.drain_timeout_ms)
            > self.max_case_duration_ms
        ):
            raise ValueError("individual timeout exceeds case duration")
        return self


class Scenario(Contract):
    schema_version: Literal["0.1"]
    scenario_id: Identifier
    scenario_version: PositiveInt
    suite: Literal["realtime"]
    category: Literal["latency", "interruption", "backchannel", "turn_taking", "pause", "overlap"]
    language: str = "zh-CN"
    tags: tuple[str, ...] = ()
    seed: NonNegativeInt
    world: World
    capabilities_required: tuple[CapabilityName, ...]
    session: SessionOptions
    audio: ScenarioAudio
    actions: Annotated[tuple[PlayAudio, ...], Field(min_length=1)]
    oracle: Oracle
    termination: Termination

    @model_validator(mode="after")
    def coherent_actions(self) -> "Scenario":
        ids = [action.action_id for action in self.actions]
        if len(ids) != len(set(ids)):
            raise ValueError("action_id must be unique within a scenario")
        for action in self.actions:
            if action.asset not in self.audio.assets:
                raise ValueError(f"action references unknown asset: {action.asset}")
            trigger = action.trigger
            if (
                isinstance(trigger, AfterEvent)
                and trigger.timeout_ms + trigger.delay_ms > self.termination.max_case_duration_ms
            ):
                raise ValueError("trigger timeout and delay exceed case duration")
            if (
                isinstance(trigger, AtOffset)
                and trigger.offset_ms >= self.termination.max_case_duration_ms
            ):
                raise ValueError("action offset must precede case deadline")
        oracle_stimulus = "utterance" if self.category == "latency" else self.category
        stimulus = (
            "utterance"
            if self.category in {"latency", "turn_taking", "pause", "overlap"}
            else self.category
        )
        if self.oracle.stimulus != oracle_stimulus:
            raise ValueError("oracle stimulus does not match scenario category")
        stimuli = {action.stimulus for action in self.actions}
        if stimulus not in stimuli or not stimuli <= {"utterance", stimulus}:
            raise ValueError("action stimuli do not match scenario category")
        if self.category in {"turn_taking", "pause", "overlap"}:
            if len(self.actions) != 1 or not isinstance(self.actions[0].trigger, SessionReady):
                raise ValueError("duplex input scenarios use a single frozen utterance")
            asset = self.audio.assets[self.actions[0].asset]
            required_kind = {"pause": "pause", "overlap": "interferer"}.get(self.category)
            if required_kind and not any(r.kind == required_kind for r in asset.regions):
                raise ValueError("duplex scenario requires annotated input regions")
        return self

    @property
    def sha256(self) -> str:
        return content_hash(self.model_dump(mode="json"))

    def model_session_options(self) -> SessionOptions:
        """Explicit allowlist boundary: no reference text, trigger or oracle reaches a model."""
        return SessionOptions.model_validate(self.session.model_dump())


class Suite(Contract):
    schema_version: Literal["0.1"]
    suite_id: Identifier
    cases: Annotated[tuple[RelativePath, ...], Field(min_length=1)]
    repetitions: PositiveInt = 1

    @model_validator(mode="after")
    def unique_paths(self) -> "Suite":
        if len(self.cases) != len(set(self.cases)):
            raise ValueError("duplicate suite paths; use repetitions instead")
        return self
