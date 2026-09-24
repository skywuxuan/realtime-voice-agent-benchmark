"""StepFun's OpenAI-compatible event dialect."""

from adapters.qwen.protocol import QwenEventMapper
from adapters.step.config import ADAPTER_VERSION, OUTPUT_FORMAT


class StepEventMapper(QwenEventMapper):
    """Normalize StepFun omissions and cancellation into the benchmark schema."""

    def __init__(self, context, sink, capabilities):
        super().__init__(
            context,
            sink,
            capabilities,
            output_format=OUTPUT_FORMAT,
            producer="adapter.step",
            vad_detector="step_server_vad",
            adapter_version=ADAPTER_VERSION,
        )
        self.active_response_id: str | None = None
        self._input_item_index = 0

    async def normalize(self, data, reading, raw_id):
        kind = data.get("type")
        if kind == "response.created":
            self.active_response_id = data.get("response", {}).get("id")
        response_id = data.get("response_id") or data.get("response", {}).get("id")
        if response_id is None:
            response_id = self.active_response_id

        if kind in {"response.thinking.delta", "response.thinking.done"}:
            return []
        if kind == "response.cancelled":
            if not response_id:
                return []
            self.active_response_id = None
            return await super().normalize(
                {
                    "type": "response.done",
                    "response": {
                        "id": response_id,
                        "status": "cancelled",
                        "status_details": {"reason": "server_cancelled"},
                    },
                },
                reading,
                raw_id,
            )
        if kind == "input_audio_buffer.committed" and not data.get("item_id"):
            self._input_item_index += 1
            data = {
                **data,
                "item_id": f"step_input_{self._input_item_index}",
            }
        if response_id and kind and kind.startswith("response.") and kind != "response.created":
            data = {**data, "response_id": response_id}
        result = await super().normalize(data, reading, raw_id)
        if kind == "response.done" and response_id == self.active_response_id:
            self.active_response_id = None
        return result
