"""Normalize observed Qwen events; preserve ambiguous associations instead of guessing."""

import base64
import json
from collections import deque
from dataclasses import dataclass, field

from adapters.qwen.config import ADAPTER_VERSION, OUTPUT_FORMAT
from events.clock import ClockReading
from events.schema import EventDraft, RecordingContext
from events.sink import AdapterArtifactSink


@dataclass
class Response:
    response_id: str
    turn_id: str | None
    association: str
    status: str = "in_progress"
    chunks: int = 0
    last_chunk_id: str | None = None
    audio_ended: bool = False
    cancel_request: str | None = None
    texts: dict[str, str] = field(default_factory=dict)
    text_counts: dict[str, int] = field(default_factory=dict)
    text_done: set[str] = field(default_factory=set)
    interruption_vad_event_id: str | None = None
    cancel_reason: str | None = None
    response_end_event_id: str | None = None
    ended_ns: int | None = None


class QwenEventMapper:
    def __init__(
        self,
        context: RecordingContext,
        sink: AdapterArtifactSink,
        capabilities,
        *,
        output_format=OUTPUT_FORMAT,
        producer="adapter.qwen",
        vad_detector="qwen_server_vad",
        adapter_version=ADAPTER_VERSION,
    ):
        self.context, self.sink, self.capabilities = context, sink, capabilities
        self.output_format = output_format
        self.producer = producer
        self.vad_detector = vad_detector
        self.adapter_version = adapter_version
        self.responses: dict[str, Response] = {}
        self.turn_mode = "manual"
        self.input_turns: set[str] = set()
        self.pending_turns: deque[str] = deque()
        self.item_turns: dict[str, str] = {}
        self.input_turn_queue: deque[str] = deque()
        self.closed_input_turns: set[str] = set()
        self._cancelled_responses: set[str] = set()
        self.tool_calls: dict[str, dict] = {}
        self.latest_input_turn: str | None = None

    def note_input(self, turn_id: str) -> None:
        self.latest_input_turn = turn_id
        if turn_id in self.closed_input_turns:
            return
        self.input_turns.add(turn_id)
        if not self.input_turn_queue or self.input_turn_queue[-1] != turn_id:
            self.input_turn_queue.append(turn_id)

    def note_commit(self, turn_id: str) -> None:
        self.pending_turns.append(turn_id)

    def _single_input_turn(self) -> str | None:
        return next(iter(self.input_turns)) if len(self.input_turns) == 1 else None

    def event(
        self,
        kind: str,
        reading: ClockReading,
        raw_id: str | None,
        payload: dict,
        *,
        source="assistant",
        basis="client_receive",
        **fields,
    ) -> EventDraft:
        return EventDraft(
            **self.context.model_dump(),
            **reading.model_dump(),
            event=kind,
            source=source,
            producer=self.producer,
            timing={"basis": basis},
            raw_event_ref=raw_id,
            payload=payload,
            **fields,
        )

    def _response(self, data: dict) -> Response:
        response_id = data.get("response_id") or data.get("response", {}).get("id")
        if response_id not in self.responses:
            raise ValueError("response event has no previously observed response.created")
        return self.responses[response_id]

    def _audio_end(self, response: Response, reading, raw_id, *, reason: str, source: str):
        if response.audio_ended or response.chunks == 0:
            return []
        response.audio_ended = True
        return [
            self.event(
                "assistant_audio_end",
                reading,
                raw_id,
                {
                    "reason": reason,
                    "last_chunk_event_id": response.last_chunk_id,
                    "complete": reason == "completed",
                    "completion_source": source,
                },
                response_id=response.response_id,
                turn_id=response.turn_id,
            )
        ]

    async def normalize(self, data: dict, reading: ClockReading, raw_id: str) -> list[EventDraft]:
        kind = data["type"]
        if kind == "response.function_call_arguments.done":
            response = self._response(data)
            call_id, name, arguments = data["call_id"], data["name"], data["arguments"]
            if not call_id or not name or not isinstance(arguments, str):
                raise ValueError("invalid function call envelope")
            signature = {"response_id": response.response_id, "name": name, "arguments": arguments}
            if call_id in self.tool_calls:
                if self.tool_calls[call_id] != signature:
                    raise ValueError("conflicting duplicate call_id")
                return []
            self.tool_calls[call_id] = signature
            try:
                parsed = json.loads(arguments)
                if not isinstance(parsed, dict):
                    parsed = None
            except ValueError:
                parsed = None
            fields = {
                "response_id": response.response_id,
                "turn_id": response.turn_id,
                "call_id": call_id,
                "item_id": data.get("item_id"),
            }
            return [
                self.event(
                    "tool_call_start",
                    reading,
                    raw_id,
                    {"name": name, "call_id": call_id, "response_id": response.response_id},
                    **fields,
                ),
                self.event(
                    "tool_call_arguments",
                    reading,
                    raw_id,
                    {
                        "representation": "final",
                        "text": arguments,
                        "parsed_arguments": parsed,
                        "parse_status": "valid" if parsed is not None else "invalid",
                    },
                    **fields,
                ),
                self.event(
                    "tool_call_end",
                    reading,
                    raw_id,
                    {
                        "name": name,
                        "arguments": parsed,
                        "valid_json": parsed is not None,
                        "completion_source": "response.function_call_arguments.done",
                    },
                    **fields,
                ),
            ]
        if kind == "session.created":
            return [
                self.event(
                    "session_start",
                    reading,
                    raw_id,
                    {
                        "vendor_session_id": data["session"]["id"],
                        "adapter_version": self.adapter_version,
                        "capabilities": self.capabilities().model_dump(mode="json"),
                    },
                    source="system",
                )
            ]
        if kind == "input_audio_buffer.committed":
            if (
                data["item_id"] in self.item_turns
                and self.item_turns[data["item_id"]] in self.closed_input_turns
            ):
                return []
            turn = self.input_turn_queue.popleft() if self.input_turn_queue else None
            if turn:
                self.item_turns[data["item_id"]] = turn
                self.closed_input_turns.add(turn)
                if self.turn_mode == "server_vad" and turn not in self.pending_turns:
                    self.pending_turns.append(turn)
            return []
        if kind in {"input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped"}:
            item_id = data.get("item_id")
            turn = self.item_turns.get(item_id) or (
                self.input_turn_queue[0] if len(self.input_turn_queue) == 1 else None
            )
            if item_id and turn:
                self.item_turns[item_id] = turn
            vad_event = self.event(
                "vad_start" if kind.endswith("started") else "vad_end",
                reading,
                raw_id,
                {
                    "detector": self.vad_detector,
                    "vendor_item_id": item_id,
                    "vendor_audio_offset_ms": data.get(
                        "audio_start_ms" if kind.endswith("started") else "audio_end_ms"
                    ),
                },
                source="user",
                turn_id=turn,
                item_id=item_id,
            )
            result = [vad_event]
            if kind.endswith("started"):
                active = [r for r in self.responses.values() if r.status == "in_progress"]
                if len(active) == 1 and active[0].turn_id != turn:
                    active[0].interruption_vad_event_id = vad_event.event_id
                # Qwen may send turn_detected cancellation immediately before VAD.
                # Only pair the latest response, a documented/observed turn reason,
                # and a short receive-order window. Never pair arbitrary old cancels.
                latest = next(reversed(self.responses.values()), None) if self.responses else None
                if (
                    not active
                    and latest
                    and latest.status == "cancelled"
                    and latest.cancel_request is None
                    and latest.cancel_reason in {"turn_detected", "server_vad"}
                    and latest.interruption_vad_event_id is None
                    and latest.ended_ns is not None
                    and 0 <= reading.timestamp_monotonic_ns - latest.ended_ns <= 1_000_000_000
                    and latest.turn_id != turn
                ):
                    latest.interruption_vad_event_id = vad_event.event_id
                    result.append(
                        self.event(
                            "interrupt_detected",
                            reading,
                            raw_id,
                            {
                                "target_response_id": latest.response_id,
                                "mechanism": "server_vad_response_cancel",
                                "evidence_event_ids": [
                                    vad_event.event_id,
                                    latest.response_end_event_id,
                                ],
                                "evidence_level": "confirmed",
                            },
                            response_id=latest.response_id,
                            turn_id=latest.turn_id,
                        )
                    )
            return result
        if kind == "conversation.item.input_audio_transcription.completed":
            item_id = data.get("item_id")
            turn = self.item_turns.get(item_id) or self._single_input_turn()
            return [
                self.event(
                    "user_text_done",
                    reading,
                    raw_id,
                    {
                        "text": data["transcript"],
                        "channel": "input_transcript",
                        "completion_source": kind,
                    },
                    source="user",
                    item_id=item_id,
                    turn_id=turn,
                )
            ]
        if kind == "response.created":
            response_id = data["response"]["id"]
            if response_id in self.responses:
                raise ValueError("duplicate response.created with a different event_id")
            turn = self.pending_turns.popleft() if self.pending_turns else None
            association = "inferred_serial_turn" if turn else "ambiguous"
            response = Response(response_id, turn, association)
            self.responses[response_id] = response
            return [
                self.event(
                    "assistant_response_start",
                    reading,
                    raw_id,
                    {
                        "response_status": data["response"].get("status", "unknown"),
                        "trigger_turn_id": turn,
                        "association_method": association,
                    },
                    response_id=response_id,
                    turn_id=turn,
                )
            ]
        if kind == "response.audio.delta":
            response = self._response(data)
            pcm = base64.b64decode(data["delta"], validate=True)
            if not pcm or len(pcm) % self.output_format.bytes_per_sample_frame:
                raise ValueError("invalid or empty Qwen PCM delta")
            if response.audio_ended and response.status != "cancelled":
                raise ValueError("audio arrived after a completed audio stream")
            reference = await self.sink.store_audio(response.response_id, pcm, self.output_format)
            chunk = self.event(
                "assistant_audio_chunk",
                reading,
                raw_id,
                {
                    "audio_ref": reference.model_dump(),
                    "chunk_index": response.chunks,
                    "late_after_cancel": response.status == "cancelled",
                    "vendor": {"after_cancel_request": response.cancel_request is not None},
                },
                response_id=response.response_id,
                turn_id=response.turn_id,
                item_id=data.get("item_id"),
                stream_id=response.response_id,
            )
            result = []
            if response.chunks == 0:
                result.append(
                    self.event(
                        "assistant_audio_start",
                        reading,
                        raw_id,
                        {
                            "first_chunk_event_id": chunk.event_id,
                            "audio_format": self.output_format.model_dump(),
                        },
                        response_id=response.response_id,
                        turn_id=response.turn_id,
                        item_id=data.get("item_id"),
                    )
                )
            response.chunks += 1
            response.last_chunk_id = chunk.event_id
            return result + [chunk]
        if kind in {"response.audio_transcript.delta", "response.text.delta"}:
            response = self._response(data)
            channel = (
                "spoken_transcript"
                if kind.startswith("response.audio_transcript")
                else "text_response"
            )
            text = data["delta"]
            if not isinstance(text, str):
                raise ValueError("text delta must be a string")
            index = response.text_counts.get(channel, 0)
            response.texts[channel] = response.texts.get(channel, "") + text
            response.text_counts[channel] = index + 1
            return [
                self.event(
                    "assistant_text_delta",
                    reading,
                    raw_id,
                    {
                        "text": text,
                        "channel": channel,
                        "delta_index": index,
                    },
                    response_id=response.response_id,
                    turn_id=response.turn_id,
                    item_id=data.get("item_id"),
                )
            ]
        if kind in {"response.audio_transcript.done", "response.text.done"}:
            response = self._response(data)
            channel = (
                "spoken_transcript"
                if kind.startswith("response.audio_transcript")
                else "text_response"
            )
            text = data.get("transcript" if channel == "spoken_transcript" else "text")
            if not isinstance(text, str):
                raise ValueError("final transcript must be a string")
            if channel in response.text_done:
                return []
            response.text_done.add(channel)
            response.texts[channel] = text
            return [
                self.event(
                    "assistant_text_done",
                    reading,
                    raw_id,
                    {
                        "text": text,
                        "channel": channel,
                        "completion_source": kind,
                        "partial": response.status == "cancelled",
                    },
                    response_id=response.response_id,
                    turn_id=response.turn_id,
                    item_id=data.get("item_id"),
                )
            ]
        if kind == "response.audio.done":
            response = self._response(data)
            return self._audio_end(
                response,
                reading,
                raw_id,
                reason="cancelled" if response.status == "cancelled" else "completed",
                source=kind,
            )
        if kind == "response.done":
            response = self._response(data)
            vendor_status = data["response"].get("status")
            response.status = (
                vendor_status
                if vendor_status in {"completed", "cancelled", "failed"}
                else "unknown"
            )
            result = []
            end = self.event(
                "assistant_response_end",
                reading,
                raw_id,
                {
                    "status": response.status,
                    "completion_source": kind,
                    "vendor": {
                        "status_details": data["response"].get("status_details"),
                        "usage": data["response"].get("usage"),
                    },
                },
                response_id=response.response_id,
                turn_id=response.turn_id,
            )
            response.response_end_event_id = end.event_id
            response.ended_ns = reading.timestamp_monotonic_ns
            details = data["response"].get("status_details") or {}
            response.cancel_reason = details.get("reason")
            result.append(end)
            if (
                response.status == "cancelled"
                and response.response_id not in self._cancelled_responses
            ):
                self._cancelled_responses.add(response.response_id)
                reason = response.cancel_reason
                initiator = (
                    "client"
                    if reason == "client_cancelled"
                    else ("server" if response.cancel_request is None else "unknown")
                )
                # Cancellation is evidence of stopping, not proof of semantic intent recognition.
                result.append(
                    self.event(
                        "assistant_cancelled",
                        reading,
                        raw_id,
                        {
                            "target_response_id": response.response_id,
                            "initiator": initiator,
                            "reason": reason or "response.done.status=cancelled",
                            "evidence": [end.event_id]
                            + ([response.cancel_request] if response.cancel_request else []),
                        },
                        response_id=response.response_id,
                        turn_id=response.turn_id,
                    )
                )
                if (
                    response.interruption_vad_event_id
                    and initiator == "server"
                    and response.cancel_request is None
                ):
                    result.append(
                        self.event(
                            "interrupt_detected",
                            reading,
                            raw_id,
                            {
                                "target_response_id": response.response_id,
                                "mechanism": "server_vad_response_cancel",
                                "evidence_event_ids": [
                                    response.interruption_vad_event_id,
                                    end.event_id,
                                ],
                                "evidence_level": "confirmed",
                            },
                            response_id=response.response_id,
                            turn_id=response.turn_id,
                        )
                    )
            result.extend(
                self._audio_end(response, reading, raw_id, reason=response.status, source=kind)
            )
            for channel, text in response.texts.items():
                if channel not in response.text_done:
                    response.text_done.add(channel)
                    result.append(
                        self.event(
                            "assistant_text_done",
                            reading,
                            raw_id,
                            {
                                "text": text,
                                "channel": channel,
                                "completion_source": "assembled_at_response_done",
                                "partial": response.status != "completed",
                            },
                            response_id=response.response_id,
                            turn_id=response.turn_id,
                        )
                    )
            return result
        if kind == "error":
            error = data.get("error", {})
            return [
                self.event(
                    "error",
                    reading,
                    raw_id,
                    {
                        "category": "vendor",
                        "code": str(error.get("code", "unknown")),
                        "message_redacted": str(error.get("message", "Qwen error")),
                        "fatal": True,
                        "scope": "session",
                        "retryable": False,
                    },
                    source="system",
                )
            ]
        # Unknown messages, input transcript deltas, item lifecycle and future tool events remain raw.
        return []
