"""The only composition layer that resolves a provider alias and its credential names."""

from dataclasses import dataclass
from pathlib import Path

from adapters.base import SessionConfig


@dataclass(frozen=True)
class AdapterRegistration:
    alias: str
    config_path: Path
    credential_variables: tuple[str, ...]

    def factory(self, config: SessionConfig):
        if self.alias == "qwen-realtime":
            from adapters.qwen import QwenRealtimeAdapter, QwenSettings

            settings = QwenSettings(model=config.model)

            def create(context, sink, clock):
                return QwenRealtimeAdapter(context, sink, settings=settings, clock=clock)

            return create
        if self.alias == "step-realtime":
            from adapters.step import StepRealtimeAdapter

            return lambda context, sink, clock: StepRealtimeAdapter(context, sink, clock=clock)
        if self.alias == "doubao-realtime":
            from adapters.doubao import DoubaoRealtimeAdapter

            return lambda context, sink, clock: DoubaoRealtimeAdapter(context, sink, clock=clock)
        if self.alias == "qwen3-omni-local":
            from adapters.opensource.qwen_omni import QwenOmniAdapter

            return lambda context, sink, clock: QwenOmniAdapter(context, sink, clock=clock)
        raise ValueError("adapter is not registered")


def resolve_adapter(alias: str) -> AdapterRegistration:
    if alias not in {"qwen-realtime", "step-realtime", "doubao-realtime", "qwen3-omni-local"}:
        raise ValueError(f"unknown adapter: {alias}")
    if alias == "qwen-realtime":
        config = "configs/qwen-realtime.yaml"
        credentials = ("DASHSCOPE_API_KEY",)
    else:
        config = f"configs/{alias}.yaml"
        credentials = ()
    return AdapterRegistration(
        alias,
        Path(__file__).resolve().parents[1] / config,
        credentials,
    )
