"""Strict, vendor-independent schemas for deterministic benchmark tools."""

from datetime import date, datetime
from typing import Literal

from pydantic import Field, model_validator

from adapters.base import ToolDefinition
from benchmark.contracts import Contract


class WeatherArguments(Contract):
    city: str = Field(min_length=1)
    date: str

    @model_validator(mode="after")
    def valid_date(self):
        if date.fromisoformat(self.date).isoformat() != self.date:
            raise ValueError("date must use YYYY-MM-DD")
        return self


class TrainArguments(Contract):
    from_city: str = Field(min_length=1)
    to_city: str = Field(min_length=1)
    date: str

    @model_validator(mode="after")
    def valid_route(self):
        if self.from_city == self.to_city:
            raise ValueError("origin and destination must differ")
        if date.fromisoformat(self.date).isoformat() != self.date:
            raise ValueError("date must use YYYY-MM-DD")
        return self


class CalendarArguments(Contract):
    operation: Literal["list", "create", "update", "delete"]
    event_id: str | None = None
    title: str | None = None
    start: str | None = None
    end: str | None = None

    @model_validator(mode="after")
    def coherent_operation(self):
        if self.operation in {"update", "delete"} and not self.event_id:
            raise ValueError("event_id required")
        if self.operation == "create" and self.event_id is not None:
            raise ValueError("create allocates event_id")
        if self.operation in {"create", "update"}:
            if not self.title or not self.start or not self.end:
                raise ValueError("title/start/end required")
            start, end = datetime.fromisoformat(self.start), datetime.fromisoformat(self.end)
            if end <= start:
                raise ValueError("event end must follow start")
        elif any(value is not None for value in (self.title, self.start, self.end)):
            raise ValueError("list/delete cannot carry event contents")
        if self.operation == "list" and self.event_id is not None:
            raise ValueError("list does not accept event_id")
        return self


ARGUMENT_MODELS = {
    "weather": WeatherArguments,
    "train": TrainArguments,
    "calendar": CalendarArguments,
}
DESCRIPTIONS = {
    "weather": "查询模拟数据库中的城市天气，日期使用 YYYY-MM-DD。",
    "train": "按出发地、目的地、日期查询模拟高铁车次。",
    "calendar": "查询或创建、更新、删除模拟日历事件。时间使用 ISO 格式。",
}


def definitions(enabled):
    if set(enabled) - ARGUMENT_MODELS.keys():
        raise ValueError("unknown enabled tool")
    return tuple(
        ToolDefinition(
            name=name,
            description=DESCRIPTIONS[name],
            parameters=ARGUMENT_MODELS[name].model_json_schema(),
        )
        for name in enabled
    )
