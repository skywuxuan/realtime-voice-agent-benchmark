"""Isolated deterministic tools with validated arguments and idempotent call IDs."""

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from benchmark.contracts import content_hash
from events.schema import ToolResult
from tools.definitions import ARGUMENT_MODELS

FailureKind = Literal["timeout", "http_500", "no_result", "permission_denied", "invalid_argument"]


@dataclass(frozen=True)
class FailureFixture:
    tool: str
    invocation: int
    kind: FailureKind

    def __post_init__(self):
        if (
            self.tool not in ARGUMENT_MODELS
            or self.invocation < 1
            or self.kind
            not in {"timeout", "http_500", "no_result", "permission_denied", "invalid_argument"}
        ):
            raise ValueError("invalid failure fixture")


@dataclass
class ToolRecord:
    call_id: str
    tool: str
    arguments: dict[str, Any]
    invocation: int
    status: str
    execution_id: str
    error: str | None = None
    result: Any = None
    state_hash: str | None = None


@dataclass
class MockToolServer:
    failures: tuple[FailureFixture, ...] = ()
    calendar_events: list[dict[str, Any]] = field(default_factory=list)
    records: list[ToolRecord] = field(default_factory=list)
    enabled: tuple[str, ...] = ("weather", "train", "calendar")
    _counts: dict[str, int] = field(default_factory=dict, init=False)
    _cache: dict[str, tuple[str, dict, ToolResult]] = field(default_factory=dict, init=False)
    _next_event: int = field(default=1, init=False)

    def __post_init__(self):
        if set(self.enabled) - ARGUMENT_MODELS.keys():
            raise ValueError("unknown enabled tool")
        schedule = [(f.tool, f.invocation) for f in self.failures]
        if len(set(schedule)) != len(schedule):
            raise ValueError("duplicate failure schedule slot")
        self.calendar_events = deepcopy(self.calendar_events)
        ids = set()
        for event in self.calendar_events:
            ARGUMENT_MODELS["calendar"].model_validate({"operation": "update", **event})
            if event["event_id"] in ids:
                raise ValueError("duplicate initial calendar event")
            ids.add(event["event_id"])
        while f"event_{self._next_event:03d}" in ids:
            self._next_event += 1

    def state(self):
        return {
            "calendar_events": deepcopy(sorted(self.calendar_events, key=lambda e: e["event_id"]))
        }

    def state_hash(self):
        return content_hash(self.state())

    def trace(self):
        return [deepcopy(asdict(record)) for record in self.records]

    def execute(self, name: str, arguments: dict, *, call_id: str) -> ToolResult:
        if not call_id:
            raise ValueError("call_id required")
        if call_id in self._cache:
            old_name, old_args, result = self._cache[call_id]
            if name != old_name or arguments != old_args:
                raise ValueError("call_id reused with different arguments")
            return result.model_copy(deep=True)
        arguments = deepcopy(arguments)
        invocation = self._counts.get(name, 0) + 1
        self._counts[name] = invocation
        execution_id = f"exec_{len(self.records) + 1:04d}"
        failure = next(
            (f.kind for f in self.failures if f.tool == name and f.invocation == invocation), None
        )
        value, error = None, None
        try:
            if name not in self.enabled:
                error = "permission_denied"
            else:
                ARGUMENT_MODELS[name].model_validate(arguments)
                if failure == "no_result":
                    value = {"found": False, "items": []}
                elif failure:
                    error = failure
                else:
                    value = self._dispatch(name, arguments)
        except (KeyError, TypeError, ValueError):
            error = "invalid_argument"
        result = ToolResult(
            call_id=call_id,
            status="error" if error else "success",
            result=deepcopy(value),
            error={"kind": error, "retryable": error in {"timeout", "http_500"}} if error else None,
            execution_id=execution_id,
            state_hash=self.state_hash(),
        )
        self.records.append(
            ToolRecord(
                call_id,
                name,
                arguments,
                invocation,
                result.status,
                execution_id,
                error,
                deepcopy(value),
                result.state_hash,
            )
        )
        self._cache[call_id] = (name, deepcopy(arguments), result.model_copy(deep=True))
        return result

    def _dispatch(self, name, args):
        if name == "weather":
            weather = {
                "北京": {"condition": "晴", "temperature_c": 24},
                "上海": {"condition": "多云", "temperature_c": 22},
                "天津": {"condition": "晴", "temperature_c": 23},
                "广州": {"condition": "小雨", "temperature_c": 26},
            }
            return {"city": args["city"], "date": args["date"], **weather[args["city"]]}
        if name == "train":
            rows = [
                {
                    "train_no": "G1",
                    "from_city": "上海",
                    "to_city": "北京",
                    "date": "2026-09-20",
                    "depart": "07:00",
                    "arrive": "11:36",
                },
                {
                    "train_no": "G2",
                    "from_city": "上海",
                    "to_city": "天津",
                    "date": "2026-09-20",
                    "depart": "08:12",
                    "arrive": "12:04",
                },
                {
                    "train_no": "G3",
                    "from_city": "上海",
                    "to_city": "北京",
                    "date": "2026-09-21",
                    "depart": "13:00",
                    "arrive": "17:38",
                },
            ]
            return {
                "trains": [
                    {
                        **r,
                        "depart_at": r["date"] + "T" + r["depart"] + ":00",
                        "arrive_at": r["date"] + "T" + r["arrive"] + ":00",
                    }
                    for r in rows
                    if all(r[k] == args[k] for k in ("from_city", "to_city", "date"))
                ]
            }
        operation = args["operation"]
        if operation == "list":
            return {"events": self.state()["calendar_events"]}
        if operation == "create":
            while any(
                e["event_id"] == f"event_{self._next_event:03d}" for e in self.calendar_events
            ):
                self._next_event += 1
            event = {
                "event_id": f"event_{self._next_event:03d}",
                **{k: args[k] for k in ("title", "start", "end")},
            }
            self._next_event += 1
            self.calendar_events.append(event)
            return {"created": deepcopy(event)}
        index = next(
            (i for i, e in enumerate(self.calendar_events) if e["event_id"] == args["event_id"]),
            None,
        )
        if index is None:
            return {"found": False}
        if operation == "delete":
            return {"deleted": self.calendar_events.pop(index)}
        self.calendar_events[index].update({k: args[k] for k in ("title", "start", "end")})
        return {"updated": deepcopy(self.calendar_events[index])}
