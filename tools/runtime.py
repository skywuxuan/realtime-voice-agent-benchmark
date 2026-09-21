"""Deterministic tool execution; optional fixed latency keeps audio IO concurrent."""

import asyncio
from collections.abc import Callable

from benchmark.contracts import content_hash
from events.clock import Clock
from events.schema import EventDraft, RecordingContext, ToolResult


class ToolRuntime:
    def __init__(
        self,
        server,
        context: RecordingContext,
        clock: Clock,
        publish: Callable[[EventDraft], None],
    ):
        self.server, self.context, self.clock, self.publish = server, context, clock, publish
        self._results: dict[str, tuple[str, ToolResult]] = {}
        self._lock = asyncio.Lock()

    def _signature(self, name, arguments, response_id):
        return content_hash({"name": name, "arguments": arguments, "response_id": response_id})

    def _cached(self, call_id, signature):
        if call_id not in self._results:
            return None
        previous, result = self._results[call_id]
        if previous != signature:
            raise ValueError("conflicting duplicate tool call")
        return result.model_copy(deep=True)

    def _emit(self, kind, call_id, response_id, payload):
        event = EventDraft(
            **self.context.model_dump(),
            **self.clock.now().model_dump(),
            event=kind,
            source="tool",
            producer="tool.runtime",
            response_id=response_id,
            call_id=call_id,
            timing={"basis": "inferred"},
            payload=payload,
        )
        self.publish(event)
        return event

    def _start(self, call_id, response_id):
        return self._emit(
            "tool_execution_start",
            call_id,
            response_id,
            {
                "execution_id": f"exec_{len(self.server.records) + 1:04d}",
                "state_version": len(self.server.records),
                "status": "started",
            },
        )

    def _complete(self, name, arguments, call_id, response_id, signature):
        result = self.server.execute(name, arguments, call_id=call_id)
        self._emit(
            "tool_execution_end",
            call_id,
            response_id,
            {
                "execution_id": result.execution_id,
                "state_version": len(self.server.records),
                "failure_fixture": result.error["kind"] if result.error else None,
                "status": result.status,
            },
        )
        self._emit("tool_result", call_id, response_id, result.model_dump(mode="json"))
        self._results[call_id] = (signature, result.model_copy(deep=True))
        return result

    def execute(self, name, arguments, *, call_id, response_id):
        if self._lock.locked():
            raise RuntimeError("cannot mix synchronous execution with pending asynchronous tools")
        signature = self._signature(name, arguments, response_id)
        result = self._cached(call_id, signature)
        if result is not None:
            return result
        self._start(call_id, response_id)
        return self._complete(name, arguments, call_id, response_id, signature)

    async def execute_async(self, name, arguments, *, call_id, response_id, delays=()):
        # FIFO serialization makes invocation-index fault schedules reproducible.
        # Only the tool worker waits; receiving and sending audio continue.
        async with self._lock:
            signature = self._signature(name, arguments, response_id)
            result = self._cached(call_id, signature)
            if result is not None:
                return result
            invocation = self.server._counts.get(name, 0) + 1
            delay = next(
                (d.delay_ms for d in delays if d.tool == name and d.invocation == invocation), 0
            )
            start = self._start(call_id, response_id)
            try:
                if delay:
                    await asyncio.sleep(delay / 1000)
                return self._complete(name, arguments, call_id, response_id, signature)
            except asyncio.CancelledError:
                self._emit(
                    "tool_execution_end",
                    call_id,
                    response_id,
                    {
                        "execution_id": start.payload.execution_id,
                        "state_version": len(self.server.records),
                        "status": "cancelled",
                    },
                )
                raise
