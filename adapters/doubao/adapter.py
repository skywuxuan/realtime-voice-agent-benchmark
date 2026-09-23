"""Seed Duplex 3.0 Realtime adapter using the verified official JSON protocol."""

import asyncio
import base64
import json
import logging
import os
import uuid
from collections.abc import Callable

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
from adapters.doubao.config import (
    ADAPTER_VERSION,
    APP_KEY,
    AUTH_DOC,
    OFFICIAL_DEMO,
    PROTOCOL_DOC,
    RESOURCE_ID,
    DoubaoSettings,
    session_create,
)
from adapters.doubao.protocol import DoubaoEventMapper
from benchmark.audio import AudioFrame
from events.clock import Clock, SystemClock
from events.redaction import Redactor
from events.schema import EventDraft, RawEvent, RecordingContext, ToolResult
from events.sink import AdapterArtifactSink


class DoubaoAdapterError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


async def _queue_until_done(queue: asyncio.Queue, done: asyncio.Event):
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


class DoubaoRealtimeAdapter(RealtimeModelAdapter):
    def __init__(
        self,
        context: RecordingContext,
        sink: AdapterArtifactSink,
        *,
        clock: Clock | None = None,
        settings: DoubaoSettings | None = None,
        connector: Callable | None = None,
    ) -> None:
        self.settings = settings or DoubaoSettings()
        super().__init__(close_timeout_s=self.settings.close_timeout_s + 2)
        self.context, self.sink = context, sink
        self.clock = clock or SystemClock()
        self._api_key = os.environ.get("BYTEDANCE_LLM_API_KEY", "").strip()
        self._app_id = os.environ.get("BYTEDANCE_LLM_APPID", "").strip()
        self._access_key = os.environ.get("BYTEDANCE_LLM_TOKEN", "").strip()
        if not self._api_key and (not self._app_id or not self._access_key):
            raise ValueError(
                "BYTEDANCE_LLM_API_KEY or both legacy App ID and Token are required"
            )
        self._redactor = Redactor(
            tuple(value for value in (self._api_key, self._app_id, self._access_key) if value)
        )
        self._connector = connector
        self._ws = None
        self._incoming: asyncio.Queue = asyncio.Queue(self.settings.queue_capacity)
        self._events: asyncio.Queue[EventDraft] = asyncio.Queue(self.settings.queue_capacity)
        self._reader_done, self._processor_done = asyncio.Event(), asyncio.Event()
        self._failed, self._closed_ack = asyncio.Event(), asyncio.Event()
        self._failure: DoubaoAdapterError | None = None
        self._reader_task = self._processor_task = None
        self._created: asyncio.Future | None = None
        self._send_lock = asyncio.Lock()
        self._closing = False
        self._session_started = False
        self._seen_vendor_ids: set[str] = set()
        self._confirmed: set[str] = set()
        self._tool_results: dict[str, dict] = {}
        self._tool_batches: dict[str, tuple[str, ...]] = {}
        self._sent_tool_batches: set[tuple[str, ...]] = set()
        self._input_muted = False
        self._idle_mute_task: asyncio.Task | None = None
        self.high_watermarks = {"incoming": 0, "events": 0}
        self.mapper = DoubaoEventMapper(context, sink, self.capabilities)

    def capabilities(self) -> CapabilityManifest:
        names = (
            "audio_input",
            "audio_output",
            "streaming_input",
            "streaming_output",
            "server_vad",
            "native_full_duplex",
            "client_cancel",
            "tool_calling",
            "tool_result_injection",
        )
        return CapabilityManifest(
            features={
                name: Capability(
                    status="supported",
                    verification="experiment" if name in self._confirmed else "docs",
                    evidence=("current session raw/normalized event log",)
                    if name in self._confirmed
                    else (PROTOCOL_DOC,),
                )
                for name in names
            }
        )

    def diagnostics(self) -> dict:
        return {
            "adapter_version": ADAPTER_VERSION,
            "endpoint": self.settings.endpoint,
            "model": "seed-duplex-3.0",
            "provider_model_version": self.settings.model_version,
            "protocol_reference": PROTOCOL_DOC,
            "authentication_reference": AUTH_DOC,
            "official_demo": OFFICIAL_DEMO,
            "transport": "websockets.asyncio-json-text",
            "capture_boundary": "complete WebSocket text message received",
            "queue_high_watermarks": dict(self.high_watermarks),
            "websocket_close_code": getattr(self._ws, "close_code", None),
            "fatal_error_code": self._failure.code if self._failure else None,
            "input_idle_mute_s": self.settings.input_idle_mute_s,
        }

    def _fail(self, code: str, message: str) -> None:
        if self._failure is None:
            clean, _ = self._redactor.clean(message)
            self._failure = DoubaoAdapterError(code, str(clean))
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

    async def _wait_created(self):
        failure_task = asyncio.create_task(self._failed.wait())
        try:
            done, _ = await asyncio.wait(
                {self._created, failure_task},
                timeout=self.settings.request_timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self._failure:
                raise self._failure
            if self._created not in done:
                raise DoubaoAdapterError("control_timeout", "session.created was not received")
            return self._created.result()
        finally:
            failure_task.cancel()
            await asyncio.gather(failure_task, return_exceptions=True)

    async def _connect(self) -> SessionInfo:
        connector = self._connector
        if connector is None:
            try:
                from websockets.asyncio.client import connect
            except ImportError:
                raise RuntimeError("install dependencies with uv sync --extra qwen") from None
            connector = connect
        headers = (
            {"X-Api-Key": self._api_key}
            if self._api_key
            else {
                "X-Api-App-Id": self._app_id,
                "X-Api-Access-Key": self._access_key,
                "X-Api-Resource-Id": RESOURCE_ID,
                "X-Api-App-Key": APP_KEY,
                "X-Api-Request-Id": str(uuid.uuid4()),
            }
        )
        try:
            self._ws = await connector(
                self.settings.endpoint,
                additional_headers=headers,
                open_timeout=self.settings.request_timeout_s,
                close_timeout=2,
                max_size=self.settings.max_message_bytes,
                max_queue=16,
                proxy=None,
                logger=logging.Logger("voice_bench.doubao.transport", level=logging.CRITICAL),
            )
        except Exception as error:
            response = getattr(error, "response", None)
            status = getattr(response, "status_code", None)
            body = getattr(response, "body", b"") or b""
            if isinstance(body, bytes):
                body = body[:1000].decode("utf-8", "replace")
            self._fail(
                "connect_failed", f"{type(error).__name__}; HTTP status={status}; {body}"
            )
            raise self._failure from None
        self._reader_task = asyncio.create_task(self._read_wire())
        self._processor_task = asyncio.create_task(self._process_wire())
        return SessionInfo(
            session_id=self.context.session_id,
            vendor_session_id=None,
            adapter_version=ADAPTER_VERSION,
        )

    async def _send_message(self, kind: str, *, command_id: str | None = None, **fields):
        async with self._send_lock:
            if self._failure:
                raise self._failure
            if self._ws is None:
                raise DoubaoAdapterError("not_connected", "no WebSocket connection")
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

    async def _configure(self, config: SessionConfig) -> EffectiveConfig:
        request = session_create(config, self.settings)
        self._created = asyncio.get_running_loop().create_future()
        await self._send_message(
            "session.create",
            session=request,
            extension={"extra": {"enable_proactive_speak": False}},
        )
        ack, reading, raw_id = await self._wait_created()
        effective = ack.get("session") or {"id": ack.get("session_id")}
        result = EffectiveConfig(
            requested=request,
            effective=effective,
            unverified={
                "session_echo": "Seed Duplex session.created does not guarantee a full config echo"
            },
        )
        self.mapper.turn_mode = config.turn_mode
        self._emit(
            self.mapper.event(
                "session_configured", reading, raw_id, result.model_dump(), source="system"
            )
        )
        return result

    async def _send_audio(self, frame: AudioFrame) -> SendReceipt:
        self.mapper.note_input(frame.turn_id)
        await self._cancel_idle_mute()
        if self._input_muted:
            await self._send_message("input_audio_unmute.commit")
            self._input_muted = False
        _, started, completed, _ = await self._send_message(
            "input_audio_buffer.append", audio=base64.b64encode(frame.pcm).decode("ascii")
        )
        self._idle_mute_task = asyncio.create_task(self._mute_after_idle())
        return SendReceipt(
            stream_id=frame.stream_id,
            chunk_index=frame.chunk_index,
            byte_count=len(frame.pcm),
            started=started,
            completed=completed,
        )

    async def _commit_turn(self, turn_id: str) -> None:
        self.mapper.note_commit(turn_id)
        await self._cancel_idle_mute()
        await self._send_message("input_audio_buffer.commit")
        await self._mute_input()

    async def _cancel_idle_mute(self) -> None:
        task, self._idle_mute_task = self._idle_mute_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _mute_after_idle(self) -> None:
        try:
            await asyncio.sleep(self.settings.input_idle_mute_s)
            await self._mute_input()
        except asyncio.CancelledError:
            return

    async def _mute_input(self) -> None:
        if not self._closing and not self._input_muted:
            await self._send_message("input_audio_mute.commit")
            self._input_muted = True

    async def _send_tool_result(self, result: ToolResult) -> None:
        call = self.mapper.tool_calls.get(result.call_id)
        if call is None:
            raise DoubaoAdapterError("unknown_call_id", "tool result has no observed call")
        data = result.model_dump(mode="json")
        if result.call_id in self._tool_results:
            if self._tool_results[result.call_id] != data:
                raise DoubaoAdapterError("conflicting_tool_result", "call result changed")
            return
        self._tool_results[result.call_id] = data
        batch = self._tool_batches.get(result.call_id, (result.call_id,))
        if batch in self._sent_tool_batches or any(cid not in self._tool_results for cid in batch):
            return
        self._sent_tool_batches.add(batch)
        items = []
        for call_id in batch:
            value = self._tool_results[call_id]
            output = (
                value["result"]
                if value["status"] == "success"
                else {"error": value["error"], "content": "座舱操作执行失败"}
            )
            items.append(
                {
                    "call_id": call_id,
                    "role": "tool",
                    "content": [
                        {
                            "type": "input_text",
                            "text": json.dumps(output, ensure_ascii=False),
                        }
                    ],
                }
            )
        response = self.mapper.responses[self.mapper.tool_calls[batch[0]]["response_id"]]
        if response.turn_id:
            self.mapper.note_commit(response.turn_id)
        await self._cancel_idle_mute()
        await self._mute_input()
        command_id, started, _, _ = await self._send_message(
            "conversation.item.create", items=items
        )
        for call_id in batch:
            call = self.mapper.tool_calls[call_id]
            value = self._tool_results[call_id]
            self._emit(
                self.mapper.event(
                    "tool_result_sent",
                    started,
                    None,
                    {"execution_id": value["execution_id"], "command_id": command_id},
                    call_id=call_id,
                    response_id=call["response_id"],
                    source="tool",
                    basis="client_send",
                )
            )

    async def _interrupt(self, request: InterruptRequest) -> CommandReceipt:
        command_id = f"cancel_{uuid.uuid4().hex}"
        reading = self.clock.now()
        self._emit(
            self.mapper.event(
                "interrupt_requested",
                reading,
                None,
                {
                    "target_response_id": request.target_response_id,
                    "command_id": command_id,
                    "reason": request.reason,
                },
                response_id=request.target_response_id,
                source="system",
                basis="client_send",
            )
        )
        _, submitted, _, _ = await self._send_message(
            "response.cancel", command_id=command_id
        )
        return CommandReceipt(command_id=command_id, submitted=submitted)

    async def _receive_event(self) -> EventDraft:
        try:
            return await _queue_until_done(self._events, self._processor_done)
        except EOFError:
            if self._failure:
                raise self._failure
            raise

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
            self._fail("raw_queue_overflow", "raw event processing could not keep up")
        except Exception as error:
            self._wire_close_type = type(error).__name__
        finally:
            self._reader_done.set()

    async def _process_wire(self) -> None:
        try:
            while True:
                try:
                    message, reading = await _queue_until_done(self._incoming, self._reader_done)
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
                    raise ValueError("received an invalid Seed Duplex event envelope")
                vendor_id = clean.get("event_id")
                if vendor_id and vendor_id in self._seen_vendor_ids:
                    continue
                if vendor_id:
                    self._seen_vendor_ids.add(vendor_id)
                if kind == "response.function_call_arguments.done":
                    items = clean.get("items") or []
                    if isinstance(items, dict):
                        items = [items]
                    batch = tuple(item.get("call_id") for item in items)
                    if not batch or any(not call_id for call_id in batch):
                        raise ValueError("function call batch has no call_id")
                    for call_id in batch:
                        self._tool_batches[call_id] = batch
                for event in await self.mapper.normalize(clean, reading, raw.raw_event_id):
                    self._emit(event)
                    if event.event == "assistant_audio_chunk":
                        self._confirmed.add("audio_output")
                        if event.payload.chunk_index > 0:
                            self._confirmed.add("streaming_output")
                    elif event.event == "user_text_done":
                        self._confirmed.add("audio_input")
                    elif event.event == "tool_call_end":
                        self._confirmed.add("tool_calling")
                if kind == "session.created" and self._created is not None:
                    self._session_started = True
                    if not self._created.done():
                        self._created.set_result((clean, reading, raw.raw_event_id))
                elif kind == "session.closed":
                    self._closed_ack.set()
                elif kind == "error":
                    error = clean.get("error") or clean
                    self._fail(
                        str(error.get("code", "vendor_error")),
                        str(error.get("message", "Seed Duplex error")),
                    )
            if not self._closing and not self._closed_ack.is_set():
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
                                "reason": "session.closed"
                                if self._closed_ack.is_set()
                                else "transport_closed",
                                "complete": self._closed_ack.is_set() and self._failure is None,
                                "last_response_ids": list(self.mapper.responses),
                            },
                            source="system",
                            basis="client_receive",
                        )
                    )
            except DoubaoAdapterError:
                pass
            self._processor_done.set()

    async def _close(self) -> None:
        self._closing = True
        await self._cancel_idle_mute()
        if self._ws is None:
            self._processor_done.set()
            return
        try:
            if self._session_started and not self._closed_ack.is_set() and not self._failure:
                await self._send_message("session.close")
                try:
                    await asyncio.wait_for(self._closed_ack.wait(), self.settings.close_timeout_s)
                except TimeoutError:
                    self._fail("close_timeout", "session.closed was not received")
            await self._ws.close()
        finally:
            for task in (self._reader_task, self._processor_task):
                if task:
                    try:
                        await asyncio.wait_for(asyncio.shield(task), 2)
                    except TimeoutError:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
