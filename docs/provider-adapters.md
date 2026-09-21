# Phase 9–11 Provider Adapter Boundaries

更新日期：2026-09-20。

`adapters/registry.py` 已注册：

| alias | 状态 | 行为 |
|---|---|---|
| `qwen-realtime` | 已实测 | WebSocket/VAD/PCM/cancel 有实测；tool result 已按固定官方示例实现，并有 Plus/weather 单场景真实工具闭环 |
| `step-realtime` | deferred | `DeferredProtocolAdapter`，能力 unknown，连接立即拒绝 |
| `doubao-realtime` | deferred | `DeferredProtocolAdapter`，能力 unknown，连接立即拒绝 |
| `qwen3-omni-local` | deferred | 本地模型 backend 边界，能力 unknown，连接立即拒绝 |

可以先生成不联网的 capability artifact：

```bash
.venv/bin/python -m benchmark.provider_probe \
  --model step-realtime --output runs/probes/step
```

该 probe 只记录 deferred 状态，不读取 key、不连接服务，也不把 unknown 能力写成 unsupported 或 supported。

Step、Doubao 和本地 Qwen3-Omni 的 endpoint、鉴权、event name、音频格式和 tool protocol 没有在当前仓库中伪造。对应 config 只有 PCM profile 和 `protocol_status: unverified`；不读取或猜测供应商 API key，也不会在默认测试中建立网络连接。

下一步接入必须在各自 adapter 内完成：

1. 固定官方文档/example commit 和实际版本。
2. 用独立 probe 验证连接、session configure、输入/输出音频格式、streaming、VAD、cancel、tool result。
3. 将 raw vendor events 映射到现有 unified schema，并加入 capability evidence。
4. 使用同一 Scenario、runner、evaluator 和 artifact layout 做一条真实 case，再开放 registry alias 的 live credentials。

本地 Qwen3-Omni 还要记录 backend commit、权重 revision、GPU、dtype、量化、模型加载方式、音频输入输出接口和是否支持真实 streaming。Transformers 整段 generate 只能进入 quality/batch track，不能伪装为 realtime latency。

2026-09-20 官方 Step 文档只读下载首次被审批服务 503 拒绝；重试获准后连接超时。未获得新的官方 wire protocol，不将 deferred 状态当阶段完成。
