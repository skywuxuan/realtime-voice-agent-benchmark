"""External tool catalogs and a deterministic acknowledgement backend."""

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from pydantic import Field, JsonValue, model_validator

from adapters.base import ToolDefinition
from benchmark.contracts import Contract, Identifier, RelativePath, Sha256, content_hash
from events.schema import ToolResult
from tools.server import ToolRecord


class ToolCatalogReference(Contract):
    catalog_id: Identifier
    path: RelativePath
    sha256: Sha256


class ToolCatalog(Contract):
    schema_version: Literal["0.1"] = "0.1"
    catalog_id: Identifier
    source: dict[str, JsonValue]
    tools: tuple[ToolDefinition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_tools(self):
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("tool catalog names must be unique")
        return self

    def definitions(self, enabled: tuple[str, ...] = ()) -> tuple[ToolDefinition, ...]:
        if not enabled:
            return self.tools
        by_name = {tool.name: tool for tool in self.tools}
        if set(enabled) - by_name.keys():
            raise ValueError("enabled tool is absent from the catalog")
        return tuple(by_name[name] for name in enabled)


def validate_arguments(schema: dict, value: Any, *, path: str = "arguments") -> None:
    """Validate the JSON Schema subset emitted by the cockpit importer."""
    expected = schema.get("type")
    valid_type = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: type(item) is int,
        "number": lambda item: type(item) in {int, float},
        "boolean": lambda item: type(item) is bool,
    }
    if expected in valid_type and not valid_type[expected](value):
        raise ValueError(f"{path} must be {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} is outside the enum")
    if expected == "object":
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        if required - value.keys():
            raise ValueError(f"{path} lacks required properties")
        if schema.get("additionalProperties") is False and set(value) - properties.keys():
            raise ValueError(f"{path} has unknown properties")
        for name, item in value.items():
            if name in properties:
                validate_arguments(properties[name], item, path=f"{path}.{name}")
    elif expected == "array" and "items" in schema:
        for index, item in enumerate(value):
            validate_arguments(schema["items"], item, path=f"{path}[{index}]")


@dataclass
class ProtocolToolServer:
    catalog: ToolCatalog
    enabled: tuple[str, ...] = ()
    records: list[ToolRecord] = field(default_factory=list)
    _counts: dict[str, int] = field(default_factory=dict, init=False)
    _cache: dict[str, tuple[str, dict, ToolResult]] = field(default_factory=dict, init=False)

    def __post_init__(self):
        self._definitions = {tool.name: tool for tool in self.catalog.definitions(self.enabled)}

    def state(self) -> dict:
        return {}

    def state_hash(self) -> str:
        return content_hash(self.state())

    def trace(self) -> list[dict]:
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
        error = None
        try:
            definition = self._definitions[name]
            validate_arguments(definition.parameters, arguments)
        except (KeyError, TypeError, ValueError):
            error = "invalid_argument" if name in self._definitions else "permission_denied"
        result_value = (
            None
            if error
            else {
                "content": "座舱操作已完成",
                "acknowledged": True,
                "function": name,
            }
        )
        result = ToolResult(
            call_id=call_id,
            status="error" if error else "success",
            result=result_value,
            error={"kind": error, "retryable": False} if error else None,
            execution_id=execution_id,
            state_hash=self.state_hash(),
        )
        self.records.append(
            ToolRecord(
                call_id=call_id,
                tool=name,
                arguments=arguments,
                invocation=invocation,
                status=result.status,
                execution_id=execution_id,
                error=error,
                result=deepcopy(result_value),
                state_hash=result.state_hash,
            )
        )
        self._cache[call_id] = (name, deepcopy(arguments), result.model_copy(deep=True))
        return result
