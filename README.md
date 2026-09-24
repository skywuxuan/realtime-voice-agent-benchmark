# Realtime Voice Agent Benchmark

面向中文实时语音 Agent 的可观测评测框架。Realtime、Agent 和 Response Quality 独立报告，原始 wire event、归一化事件、PCM、工具调用和离线评价均保存为可重放工件。

当前 Qwen live 路径只支持 Qwen Audio 3.0 Realtime Flash/Plus；默认配置为 Flash、`longanqian`、16 kHz PCM 输入、24 kHz PCM 输出和 `smart_turn`。

## 安装与检查

```bash
uv sync --locked --python 3.11 --extra qwen
uv run --locked ruff check .
uv run --locked pytest -q
```

如果默认 uv cache 不可写，可以设置 `UV_CACHE_DIR=/tmp/voice-bench-uv-cache`。默认测试不访问收费模型服务。

## 核心模块

| 模块 | 职责 |
|---|---|
| `events/` | 事件 schema、双时钟、脱敏、录制、PCM 引用和封口回放 |
| `scenarios/` | latency、interruption、backchannel、duplex 和 Agent 场景契约 |
| `adapters/qwen/` | Qwen Audio 3.0 WebSocket、smart-turn、取消、工具调用和结果回注 |
| `adapters/doubao/` | Seed Duplex 3.0 官方 JSON Realtime 协议、工具调用和结果回注 |
| `simulator/` | 绝对 deadline 输入调度、语音边界和虚拟播放 |
| `dataset/`、`renderers/` | 文本数据校验、会话前 TTS、不可变内容缓存和场景编译 |
| `benchmark/`、`evaluator/` | 在线运行、离线有效性检查、分母、指标和版本化重评 |
| `tools/`、`agent/` | 确定性工具、真实模型调用驱动、状态重放和 Task Completion |
| `reports/` | 自包含 HTML 报告 |

Step 保留显式 deferred 边界。Seed Duplex 3.0 协议按官方文档实现；真实运行需要有效的
服务权限，鉴权失败会作为连接错误写入本地工件。

## 离线 Smoke

```bash
uv run --locked python -m benchmark.smoke --output runs/my-phase1-smoke
uv run --locked python -m scenarios.validate \
  runs/my-phase1-smoke/scenario.yaml --asset-root runs/my-phase1-smoke
```

该示例使用脚本化 Adapter 和合成音调，只验证 Scenario、事件、音频引用、封口哈希和回放，不产生模型分数。

## 文本输入

文本 corpus 在连接目标模型之前渲染为冻结音频：

```bash
.venv/bin/python -m benchmark.run \
  --model qwen-realtime --suite realtime \
  --scenario datasets/source/half_duplex/corpus.yaml \
  --tts-profile configs/tts/qwen-cherry.yaml \
  --render-missing --warmups 0
```

正式复跑应去掉 `--render-missing`；缺失缓存会在任何模型调用前失败。格式、缓存身份和 full-duplex 触发器见 [文本数据说明](docs/text-dataset.md)。

## 智能座舱工具数据

`dataset.cockpit` 将外部工具协议和 case JSONL 编译为冻结 TTS、工具 catalog 和半双工
Agent suite。期望调用只进入 evaluator，不会作为额外文本提示交给模型。

单工具候选面适合调试参数抽取，但不能评价多工具路由；完整候选面或领域候选组必须作为
独立拓扑报告。数据格式、编译命令和分组限制见
[智能座舱 Benchmark](docs/cockpit-benchmark.md)。

## 通用 Realtime 与 Agent

Realtime suite：

```bash
.venv/bin/python -m benchmark.run \
  --model qwen-realtime --suite realtime \
  --config configs/qwen-audio-3.0-realtime-flash-agent.yaml \
  --scenario scenarios/realtime/basic.yaml \
  --output runs/my-realtime-run

.venv/bin/python -m benchmark.evaluate --run runs/my-realtime-run
```

Agent runtime 只执行模型实际产生的 `tool_call_end`。期望调用、期望状态和禁止调用只供 evaluator 使用，不能进入模型公开配置。详见 [Agent 实现](docs/agent-implementation.md)。

## 凭据与工件

真实运行要求进程环境存在 `DASHSCOPE_API_KEY`。程序不自动读取 `.env`，也不接受命令行明文 key。

每次运行必须使用新输出目录。Agent 大批量运行可使用 `--artifact-mode compact`；运行与
评价完成后生成可读 `results.json` 和校验过的 `evidence.tar.gz`，再移除松散 case 文件。
归档保留原始 config、scenario、raw/normalized events、PCM、transcript、工具记录和历史评价，
可用 `python -m agent.artifacts restore <run>` 恢复。冻结 TTS 缓存位于
`datasets/rendered/`，不随 run 压缩或清理。所有运行目录、外部数据、转换产物和报告均为
本地工件，不进入版本控制。

多分片 campaign 封口后可进一步合并为 campaign bundle，减少 `runs/` 下的目录数量；bundle
保留每个原始 run 的名称、索引和证据归档。cockpit campaign 默认每 500 个源行一个分片，
需要更细粒度断点时可用 `--batch-size` 调小。

主要设计文档：

- [事件格式](docs/event-schema.md)
- [架构](docs/architecture.md)
- [评测计划](docs/benchmark-plan.md)
- [Qwen Audio 3.0 接入](docs/qwen-integration.md)
- [Provider 边界](docs/provider-adapters.md)
