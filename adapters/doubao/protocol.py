"""Normalize Seed Duplex 3.0 Realtime events into the benchmark schema."""

import uuid

from adapters.doubao.config import ADAPTER_VERSION
from adapters.qwen.protocol import QwenEventMapper


class DoubaoEventMapper(QwenEventMapper):
    def __init__(self, context, sink, capabilities):
        super().__init__(context, sink, capabilities)
        self.active_response_id = None
        self._input_item_index = 0
        self._tool_response_ids = set()

    def event(self, *args, **kwargs):
        return super().event(*args, **kwargs).model_copy(update={"producer": "adapter.doubao"})

    async def _ensure_response(self, data, reading, raw_id):
        response_id = data.get("response_id") or data.get("response", {}).get("id")
        if response_id is None:
            active = self.responses.get(self.active_response_id)
            if active is not None and active.status == "in_progress":
                response_id = active.response_id
            else:
                response_id = "seed_response_" + uuid.uuid4().hex
        if response_id in self.responses:
            self.active_response_id = response_id
            return response_id, []
        self.active_response_id = response_id
        events = await super().normalize(
            {
                "type": "response.created",
                "event_id": data.get("event_id"),
                "response": {"id": response_id, "status": "in_progress"},
            },
            reading,
            raw_id,
        )
        return response_id, events

    async def normalize(self, data, reading, raw_id):
        kind = data["type"]
        if kind == "session.created":
            return [
                self.event(
                    "session_start",
                    reading,
                    raw_id,
                    {
                        "vendor_session_id": data["session"]["id"],
                        "adapter_version": ADAPTER_VERSION,
                        "capabilities": self.capabilities().model_dump(mode="json"),
                    },
                    source="system",
                )
            ]
        if kind == "input_audio_buffer.committed":
            self._input_item_index += 1
            data = {
                **data,
                "item_id": data.get("item_id") or f"seed_input_{self._input_item_index}",
            }
            return await super().normalize(data, reading, raw_id)
        if kind == "conversation.item.input_audio_transcription.completed":
            data = {**data, "transcript": data.get("transcript", data.get("text", ""))}
            item_id = data.get("item_id")
            turn = self.item_turns.get(item_id) or self._single_input_turn()
            if item_id and turn:
                self.item_turns[item_id] = turn
            if turn and self.turn_mode == "server_vad" and turn not in self.pending_turns:
                self.pending_turns.append(turn)
                self.closed_input_turns.add(turn)
            return await super().normalize(data, reading, raw_id)
        response_kinds = {
            "response.output_audio.started",
            "response.output_audio.delta",
            "response.output_audio.done",
            "response.output_text.delta",
            "response.output_text.done",
            "response.function_call_arguments.done",
            "response.done",
            "response.canceled",
        }
        if kind not in response_kinds:
            return await super().normalize(data, reading, raw_id)
        if kind == "response.done":
            completed_id = (
                data.get("response_id")
                or data.get("response", {}).get("id")
                or self.active_response_id
            )
            if completed_id in self._tool_response_ids:
                self._tool_response_ids.remove(completed_id)
                return []
        response_id, started = await self._ensure_response(data, reading, raw_id)
        if kind == "response.output_audio.started":
            return started
        if kind == "response.output_audio.delta":
            translated = {
                **data,
                "type": "response.audio.delta",
                "response_id": response_id,
                "delta": data.get("audio", data.get("delta")),
            }
            return started + await super().normalize(translated, reading, raw_id)
        if kind == "response.output_audio.done":
            translated = {**data, "type": "response.audio.done", "response_id": response_id}
            return started + await super().normalize(translated, reading, raw_id)
        if kind == "response.output_text.delta":
            translated = {
                **data,
                "type": "response.text.delta",
                "response_id": response_id,
                "delta": data.get("delta", data.get("text", "")),
            }
            return started + await super().normalize(translated, reading, raw_id)
        if kind == "response.output_text.done":
            translated = {
                **data,
                "type": "response.text.done",
                "response_id": response_id,
                "text": data.get("text", data.get("content", "")),
            }
            return started + await super().normalize(translated, reading, raw_id)
        if kind == "response.function_call_arguments.done":
            items = data.get("items") or []
            if isinstance(items, dict):
                items = [items]
            events = list(started)
            for item in items:
                function = item.get("function") or {}
                translated = {
                    "type": kind,
                    "event_id": data.get("event_id"),
                    "response_id": response_id,
                    "item_id": item.get("item_id"),
                    "call_id": item.get("call_id"),
                    "name": item.get("name") or function.get("name"),
                    "arguments": item.get("arguments") or function.get("arguments") or "{}",
                }
                events.extend(await super().normalize(translated, reading, raw_id))
            self._tool_response_ids.add(response_id)
            events.extend(
                await super().normalize(
                    {
                        "type": "response.done",
                        "event_id": data.get("event_id"),
                        "response": {
                            "id": response_id,
                            "status": "completed",
                            "status_details": {
                                "reason": "function_call_arguments_done"
                            },
                        },
                    },
                    reading,
                    raw_id,
                )
            )
            return events
        status = "cancelled" if kind == "response.canceled" else "completed"
        translated = {
            "type": "response.done",
            "event_id": data.get("event_id"),
            "response": {
                "id": response_id,
                "status": status,
                "status_details": data.get("status_details"),
                "usage": data.get("usage"),
            },
        }
        return started + await super().normalize(translated, reading, raw_id)
