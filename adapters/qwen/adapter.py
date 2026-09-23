"""Async Qwen transport. All vendor protocol stays inside this package."""

import asyncio
import base64
import json
import logging
import os
import uuid
from collections.abc import Callable
from urllib.parse import urlencode

from adapters.base import (
    Capability,
    CapabilityManifest,
    CommandReceipt,
    EffectiveConfig,
    InterruptRequest,
    RealtimeModelAdapter,
    SendReceipt,
    SessionConfig,
    SessionInfo,
)
from adapters.qwen.config import (
    ADAPTER_VERSION,
    SDK_SOURCE,
    QwenSettings,
    session_update,
)
from adapters.qwen.protocol import QwenEventMapper
from benchmark.audio import AudioFrame
from events.clock import Clock, SystemClock
from events.redaction import Redactor
from events.schema import EventDraft, RawEvent, RecordingContext, ToolResult
from events.sink import AdapterArtifactSink


class QwenAdapterError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


async def queue_until_done(queue: asyncio.Queue, done: asyncio.Event):
    """Drain queued items before EOF, including a simultaneous queue/end notification."""
    if not queue.empty():
        return queue.get_nowait()
    if done.is_set():
        raise EOFError
    get_task = asyncio.create_task(queue.get())
    end_task = asyncio.create_task(done.wait())
    try:
        await asyncio.wait({get_task, end_task}, return_when=asyncio.FIRST_COMPLETED)
        if get_task.done():
            return get_task.result()
        if not queue.empty():
            return queue.get_nowait()
        raise EOFError
    finally:
        for task in (get_task, end_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(get_task, end_task, return_exceptions=True)


class QwenRealtimeAdapter(RealtimeModelAdapter):
    def __init__(
        self,
        context: RecordingContext,
        sink: AdapterArtifactSink,
        *,
        settings: QwenSettings | None = None,
        clock: Clock | None = None,
        connector: Callable | None = None,
    ) -> None:
        self.settings = settings or QwenSettings()
        super().__init__(close_timeout_s=self.settings.close_timeout_s + 3)
        self.context, self.sink = context, sink
        self.clock = clock or SystemClock()
        self._api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        if not self._api_key:
            raise ValueError("DASHSCOPE_API_KEY must be set in the environment")
        self._redactor = Redactor((self._api_key,))
        self._connector = connector
        self._ws = None
        self._incoming: asyncio.Queue = asyncio.Queue(self.settings.queue_capacity)
        self._events: asyncio.Queue[EventDraft] = asyncio.Queue(self.settings.queue_capacity)
        self._reader_done, self._processor_done = asyncio.Event(), asyncio.Event()
        self._failed, self._finished = asyncio.Event(), asyncio.Event()
        self._failure: QwenAdapterError | None = None
        self._reader_task = self._processor_task = None
        self._created = self._updated = None
        self._send_lock = asyncio.Lock()
        self._closing = False
        self._session_started = False
        self._seen_vendor_ids: set[str] = set()
        self._confirmed: set[str] = set()
        self._followup_response_waiter: asyncio.Future | None = None
        self.high_watermarks = {"incoming": 0, "events": 0}
        self.effective_config: EffectiveConfig | None = None
        self.mapper = QwenEventMapper(context, sink, self.capabilities)
        self._tool_results: dict[str, dict] = {}

    def capabilities(self) -> CapabilityManifest:
        features = {}
        for name in (
            "audio_input",
            "audio_output",
            "streaming_input",
            "streaming_output",
            "server_vad",
            "client_cancel",
            "tool_calling",
            "tool_result_injection",
        ):
            verified = name in self._confirmed
            features[name] = Capability(
                status="supported",
                verification="experiment" if verified else "docs",
                evidence=("current session raw/normalized event log",)
                if verified
                else (SDK_SOURCE,),
            )
        return CapabilityManifest(features=features)

    def diagnostics(self) -> dict:
        return {
            "adapter_version": ADAPTER_VERSION,
            "endpoint": self.settings.endpoint,
            "model": self.settings.model,
            "protocol_reference": SDK_SOURCE,
            "api_version": None,
            "api_version_reason": "not exposed by the realtime session",
            "model_revision": None,
            "model_revision_reason": "model alias only; no snapshot returned",
            "transport": "websockets.asyncio",
            "capture_boundary": "complete WebSocket message received",
            "queue_high_watermarks": dict(self.high_watermarks),
            "close_strategy": "websocket_close",
            "websocket_close_code": getattr(self._ws, "close_code", None),
            "server_finish_seen": self._finished.is_set(),
            "fatal_error_code": self._failure.code if self._failure else None,
            "audio3_response_policy": "server_smart_turn"
            if self.config is None or self.config.turn_mode == "server_vad"
            else "client_create_after_commit",
        }

    def _fail(self, code: str, message: str) -> None:
        if self._failure is None:
            clean, _ = self._redactor.clean(message)
            self._failure = QwenAdapterError(code, str(clean))
            self._failed.set()

    def _emit(self, event: EventDraft) -> None:
        try:
            self._events.put_nowait(event)
            self.high_watermarks["events"] = max(
                self.high_watermarks["events"], self._events.qsize()
            )
        except asyncio.QueueFull:
            self._fail("event_queue_overflow", "consumer did not drain normalized events")
            raise self._failure

    async def _wait_control(self, future: asyncio.Future):
        failure_task = asyncio.create_task(self._failed.wait())
        try:
            done, _ = await asyncio.wait(
                {future, failure_task},
                timeout=self.settings.request_timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self._failure:
                raise self._failure
            if future not in done:
                raise QwenAdapterError("control_timeout", "server did not acknowledge the request")
            return future.result()
        finally:
            failure_task.cancel()
            await asyncio.gather(failure_task, return_exceptions=True)

    async def _connect(self) -> SessionInfo:
        connector = self._connector
        if connector is None:
            try:
                from websockets.asyncio.client import connect
            except ImportError:
                raise RuntimeError("install the Qwen extra: uv sync --extra qwen") from None
            connector = connect
        self._created = asyncio.get_running_loop().create_future()
        url = self.settings.endpoint + "?" + urlencode({"model": self.settings.model})
        try:
            self._ws = await connector(
                url,
                additional_headers={"Authorization": "Bearer " + self._api_key},
                open_timeout=self.settings.request_timeout_s,
                close_timeout=2,
                max_size=self.settings.max_message_bytes,
                max_queue=16,
                proxy=None,
                logger=logging.Logger("voice_bench.qwen.transport", level=logging.CRITICAL),
            )
        except Exception as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            raise QwenAdapterError(
                "connect_failed", f"{type(error).__name__}; HTTP status={status}"
            ) from None
        self._reader_task = asyncio.create_task(self._read_wire())
        self._processor_task = asyncio.create_task(self._process_wire())
        data, _, _ = await self._wait_control(self._created)
        return SessionInfo(
            session_id=self.context.session_id,
            vendor_session_id=data["session"]["id"],
            adapter_version=ADAPTER_VERSION,
        )

    async def _send_message(self, kind: str, *, command_id: str | None = None, **fields):
        async with self._send_lock:
            if self._failure:
                raise self._failure
            if self._ws is None:
                raise QwenAdapterError("not_connected", "no WebSocket connection")
            body = {"event_id": command_id or f"cmd_{uuid.uuid4().hex}", "type": kind, **fields}
            reading = self.clock.now()
            try:
                await self._ws.send(json.dumps(body, ensure_ascii=False, allow_nan=False))
            except Exception as error:
                self._fail("send_failed", type(error).__name__)
                raise self._failure from None
            completed = self.clock.now()
            clean, removed = self._redactor.clean(body)
            raw = await self.sink.record_raw(
                RawEvent(
                    **self.context.model_dump(),
                    **reading.model_dump(),
                    direction="sent",
                    transport="websocket",
                    vendor_event_type=kind,
                    body=clean,
                    redacted_fields=removed,
                )
            )
            return body["event_id"], reading, completed, raw.raw_event_id

    def _response_options(self, *, tool_followup: bool = False) -> dict:
        options = {"modalities": ["audio", "text"]}
        if self.config is not None and self.config.tools:
            options["tool_choice"] = (
                str(self.config.provider_options.get("tool_followup_choice", "auto"))
                if tool_followup
                else "auto"
            )
        return options

    async def _create_tool_followup_response(self) -> None:
        delays = (0.0, 1.2, 2.6, 5.0)
        for attempt, delay in enumerate(delays):
            if delay:
                await asyncio.sleep(delay)
            waiter = asyncio.get_running_loop().create_future()
            self._followup_response_waiter = waiter
            try:
                await self._send_message(
                    "response.create", response=self._response_options(tool_followup=True)
                )
                accepted = await self._wait_control(waiter)
            finally:
                if self._followup_response_waiter is waiter:
                    self._followup_response_waiter = None
            if accepted:
                return
            if attempt == len(delays) - 1:
                break
        raise QwenAdapterError("response_slot_busy", "tool follow-up response remained busy")

    async def _configure(self, config: SessionConfig) -> EffectiveConfig:
        request = session_update(config, self.settings)
        self._updated = asyncio.get_running_loop().create_future()
        await self._send_message("session.update", session=request)
        ack, reading, raw_id = await self._wait_control(self._updated)
        effective = ack["session"]
        if (effective.get("turn_detection") is None) != (config.turn_mode == "manual"):
            raise QwenAdapterError(
                "configuration_mismatch", "server did not confirm the requested turn mode"
            )
        unverified = {
            key: value
            for key, value in request.items()
            if key not in effective or effective[key] != value
        }
        strict_echoes = ["voice", "output_audio_format"]
        for key in strict_echoes:
            if key in unverified:
                raise QwenAdapterError(
                    "configuration_mismatch", f"server did not echo requested {key}"
                )
        if "input_audio_format" in unverified:
            unverified["input_audio_format"] = {
                "requested": request["input_audio_format"],
                "effective": "not echoed by Audio 3.0 session.updated",
                "basis": "model profile and accepted session.update",
            }
        unverified["audio_sample_rates"] = {
            "input_hz": 16000,
            "output_hz": 24000,
            "basis": "official PCM profile; not explicit in session echo",
        }
        result = EffectiveConfig(requested=request, effective=effective, unverified=unverified)
        self.effective_config = result
        self.mapper.turn_mode = config.turn_mode
        self._emit(
            self.mapper.event(
                "session_configured", reading, raw_id, result.model_dump(), source="system"
            )
        )
        return result

    async def _send_audio(self, frame: AudioFrame) -> SendReceipt:
        self.mapper.note_input(frame.turn_id)
        _, started, completed, _ = await self._send_message(
            "input_audio_buffer.append", audio=base64.b64encode(frame.pcm).decode("ascii")
        )
        return SendReceipt(
            stream_id=frame.stream_id,
            chunk_index=frame.chunk_index,
            byte_count=len(frame.pcm),
            started=started,
            completed=completed,
        )

    async def _commit_turn(self, turn_id: str) -> None:
        self.mapper.note_commit(turn_id)
        await self._send_message("input_audio_buffer.commit")
        await self._send_message("response.create", response=self._response_options())

    async def _interrupt(self, request: InterruptRequest) -> CommandReceipt:
        active = [
            response
            for response in self.mapper.responses.values()
            if response.status == "in_progress"
        ]
        # The verified Qwen cancel request targets the current response, not an arbitrary ID.
        if len(active) != 1 or active[0].response_id != request.target_response_id:
            raise QwenAdapterError(
                "cancel_target_mismatch", "requested response is not the sole active response"
            )
        command_id = f"cancel_{uuid.uuid4().hex}"
        requested = self.mapper.event(
            "interrupt_requested",
            self.clock.now(),
            None,
            {
                "target_response_id": request.target_response_id,
                "command_id": command_id,
                "reason": request.reason,
            },
            source="system",
            response_id=request.target_response_id,
            basis="client_send",
        )
        self._emit(requested)
        active[0].cancel_request = requested.event_id
        _, started, _, _ = await self._send_message("response.cancel", command_id=command_id)
        return CommandReceipt(command_id=command_id, submitted=started)

    async def _receive_event(self) -> EventDraft:
        try:
            return await queue_until_done(self._events, self._processor_done)
        except EOFError:
            if self._failure:
                raise self._failure
            raise

    async def _send_tool_result(self, result: ToolResult) -> None:
        call = self.mapper.tool_calls.get(result.call_id)
        if call is None:
            raise QwenAdapterError("unknown_call_id", "tool result has no observed call")
        data = result.model_dump(mode="json")
        if result.call_id in self._tool_results:
            if self._tool_results[result.call_id] != data:
                raise QwenAdapterError("conflicting_tool_result", "call result changed")
            return
        response = self.mapper.responses[call["response_id"]]
        if response.status != "completed":
            raise QwenAdapterError(
                "tool_response_not_completed", "wait for completed tool response"
            )
        # Reserve before sending: an uncertain transport failure must not resend a side effect.
        self._tool_results[result.call_id] = data
        pending = [
            cid
            for cid, value in self.mapper.tool_calls.items()
            if value["response_id"] == response.response_id and cid not in self._tool_results
        ]
        superseded = self.mapper.latest_input_turn not in {None, response.turn_id}
        if not pending and not superseded and response.turn_id:
            self.mapper.note_commit(response.turn_id)
        output = (
            result.result
            if result.status == "success"
            else {"error": result.error, "content": "座舱操作执行失败"}
        )
        command_id, started, _, _ = await self._send_message(
            "conversation.item.create",
            item={
                "id": "tool_" + uuid.uuid4().hex,
                "type": "function_call_output",
                "call_id": result.call_id,
                "output": json.dumps(output, ensure_ascii=False),
            },
        )
        self._emit(
            self.mapper.event(
                "tool_result_sent",
                started,
                None,
                {
                    "execution_id": result.execution_id,
                    "command_id": command_id,
                    "vendor": {"resume_policy": "latest_input_turn", "superseded": superseded},
                },
                call_id=result.call_id,
                response_id=response.response_id,
                turn_id=response.turn_id,
                source="tool",
                basis="client_send",
            )
        )
        # A newer real audio turn owns the next response. Inject the old tool
        # result into history, but do not start another answer to the old turn.
        if not pending and not superseded:
            await self._create_tool_followup_response()

    async def _read_wire(self) -> None:
        try:
            while True:
                message = await self._ws.recv()
                reading = self.clock.now()
                self._incoming.put_nowait((message, reading))
                self.high_watermarks["incoming"] = max(
                    self.high_watermarks["incoming"], self._incoming.qsize()
                )
        except asyncio.CancelledError:
            raise
        except asyncio.QueueFull:
            self._fail(
                "raw_queue_overflow", "raw event processing could not keep up with reception"
            )
        except Exception as error:
            # Normal close and unexpected disconnect are distinguished after all queued events drain.
            self._wire_close_type = type(error).__name__
        finally:
            self._reader_done.set()

    async def _process_wire(self) -> None:
        try:
            while True:
                try:
                    message, reading = await queue_until_done(self._incoming, self._reader_done)
                except EOFError:
                    break
                try:
                    data = json.loads(message)
                except (ValueError, TypeError, UnicodeError):
                    data = {"unparsed_message": str(message)}
                if not isinstance(data, dict):
                    data = {"unparsed_message": data}
                clean, removed = self._redactor.clean(data)
                kind = clean.get("type")
                raw = await self.sink.record_raw(
                    RawEvent(
                        **self.context.model_dump(),
                        **reading.model_dump(),
                        direction="received",
                        transport="websocket",
                        vendor_event_type=kind if isinstance(kind, str) else None,
                        body=clean,
                        redacted_fields=removed,
                    )
                )
                if not isinstance(kind, str):
                    raise ValueError("received an invalid vendor event envelope")
                vendor_id = clean.get("event_id")
                if vendor_id and vendor_id in self._seen_vendor_ids:
                    continue  # Keep every raw delivery but never duplicate normalized audio.
                if vendor_id:
                    self._seen_vendor_ids.add(vendor_id)
                error = clean.get("error", {}) if kind == "error" else {}
                busy_followup = (
                    kind == "error"
                    and self._followup_response_waiter is not None
                    and not self._followup_response_waiter.done()
                    and "another response is in progress"
                    in str(error.get("message", "")).lower()
                )
                if busy_followup:
                    self._followup_response_waiter.set_result(False)
                    continue
                for event in await self.mapper.normalize(clean, reading, raw.raw_event_id):
                    self._emit(event)
                    if event.event == "assistant_audio_chunk":
                        self._confirmed.add("audio_output")
                        if event.payload.chunk_index > 0:
                            self._confirmed.add("streaming_output")
                    if event.event == "user_text_done":
                        self._confirmed.add("audio_input")
                    if event.event == "tool_call_end":
                        self._confirmed.add("tool_calling")
                    if event.event in {"vad_start", "vad_end"}:
                        self._confirmed.add("server_vad")
                    if event.event == "assistant_cancelled" and event.payload.initiator == "client":
                        self._confirmed.add("client_cancel")
                if kind == "session.created":
                    self._session_started = True
                    if not self._created.done():
                        self._created.set_result((clean, reading, raw.raw_event_id))
                elif (
                    kind == "session.updated"
                    and self._updated is not None
                    and not self._updated.done()
                ):
                    self._updated.set_result((clean, reading, raw.raw_event_id))
                elif kind == "session.finished":
                    self._finished.set()
                elif (
                    kind == "response.created"
                    and self._followup_response_waiter is not None
                    and not self._followup_response_waiter.done()
                ):
                    self._followup_response_waiter.set_result(True)
                elif kind == "error":
                    self._fail(
                        str(error.get("code", "vendor_error")),
                        str(error.get("message", "Qwen error")),
                    )
            if not self._closing and not self._finished.is_set():
                self._fail("connection_closed", getattr(self, "_wire_close_type", "EOF"))
        except asyncio.CancelledError:
            self._fail("processor_cancelled", "normalization did not finish")
            raise
        except Exception as error:
            self._fail("protocol_or_recording_error", type(error).__name__)
        finally:
            try:
                if self._failure:
                    self._emit(
                        self.mapper.event(
                            "error",
                            self.clock.now(),
                            None,
                            {
                                "category": "adapter",
                                "code": self._failure.code,
                                "message_redacted": str(self._failure),
                                "fatal": True,
                                "scope": "session",
                                "retryable": False,
                            },
                            source="system",
                            basis="inferred",
                        )
                    )
                if self._session_started:
                    self._emit(
                        self.mapper.event(
                            "session_end",
                            self.clock.now(),
                            None,
                            {
                                "reason": "client_websocket_close"
                                if self._closing
                                else "transport_closed",
                                "complete": self._closing
                                and self._failure is None
                                and getattr(self._ws, "close_code", None) in {1000, 1001},
                                "last_response_ids": list(self.mapper.responses),
                            },
                            source="system",
                            basis="inferred",
                        )
                    )
            except QwenAdapterError:
                pass  # Queue overflow is already a fatal, surfaced error, never a passing run.
            self._processor_done.set()

    async def _close(self) -> None:
        self._closing = True
        if self._ws is None:
            self._processor_done.set()
            return
        try:
            await self._ws.close()
        finally:
            for task in (self._reader_task, self._processor_task):
                if task:
                    try:
                        await asyncio.wait_for(asyncio.shield(task), 2)
                    except TimeoutError:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
