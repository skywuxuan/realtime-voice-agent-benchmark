# Realtime Voice Agent Benchmark

面向中文完整 Realtime Voice Agent 的评测框架。Realtime、Agent、Response Quality 独立报告。

截至 2026-09-21，已有真实 Qwen latency、中断、5 个停顿变体及中文语音工具闭环工件。文本测试集可在运行前由独立 Qwen TTS renderer 编译为不可变音频，半双工、全双工中断与显式停顿均已完成真实链路验证。Backchannel 已准备“嗯嗯”“你继续”冻结 TTS，并与旧音调实验分开运行。Step、Doubao、本地 Omni 仍是占位 adapter，所有阶段尚未全部完成。

开发环境为 Python 3.11，使用 uv 管理依赖；`uv.lock` 固定版本。Qwen 使用可选的 `websockets==15.0.1` 异步 transport，协议来自官方 SDK/示例及实际会话核验，无 GPU 依赖。

## 安装与检查

```bash
uv sync --locked --python 3.11 --extra qwen
uv run --locked pytest -q
uv run --locked ruff check .
```

如果默认 uv cache 不可写，可为命令设置 `UV_CACHE_DIR=/tmp/voice-bench-uv-cache`。本次执行环境的普通沙箱会阻塞 asyncio 线程退出；离线测试已在允许线程唤醒的受控环境运行，未调用模型 API。

## 离线录制与回放示例

```bash
uv run --locked python -m benchmark.smoke --output runs/my-phase1-smoke
uv run --locked python -m scenarios.validate runs/my-phase1-smoke/scenario.yaml --asset-root runs/my-phase1-smoke
```

示例使用 `ScriptedAdapter` 与 20 ms 合成音调，验证 Scenario → Adapter 契约 → raw/normalized events → PCM 工件 → 完整性校验 → 离线回读。音频不是中文语音，不按实时节奏发送，不产生 TTFA 或模型分数。每次选择新的输出目录，已有目录会被拒绝覆盖。

```text
runs/my-phase1-smoke/
  scenario.yaml
  fixture.wav
  recording/
    config.json
    manifest.json
    raw_events.jsonl
    events.jsonl
    audio/*.pcm
```

回读接口为 `events.replay.read_recording(path)`，默认验证文件哈希、事件序列/上下文、音频引用及结束标记。`allow_partial=True` 仅用于检查失败或中断留下的工件；不能把它视为完整 benchmark 结果。manifest 的 `complete` 指录制文件完整，模型是否成功由后续 evaluator 决定。

## 当前模块

| 模块 | 已实现 |
|---|---|
| `events/schema.py` / `clock.py` | v0.1 envelope、所有约定事件名及 payload 校验、双时钟与时钟域检查 |
| `events/recorder.py` / `replay.py` | 异步串行写入、原始事件、PCM/blob、脱敏、哈希 manifest、完整/partial 回读 |
| `events/bus.py` | 有界事件分发；满队列时报错，避免只投递给部分订阅者 |
| `scenarios/schema.py` / `loader.py` | latency/interruption/backchannel 场景与 suite；ID、触发器、WAV/标注/hash 校验 |
| `adapters/base.py` | 统一异步接口、能力证据、生命周期、manual/VAD 提交约束、单接收者、幂等关闭 |
| `adapters/testing.py` | 仅供离线测试的脚本化 transport |
| `adapters/qwen/` | 真实 Qwen WebSocket、配置确认、流式音频/转写、VAD、取消、异常关闭与事件归一化 |
| `benchmark/connection_probe.py` | 与厂商协议无关的单会话音频连通性验证、工件导出 |
| `benchmark/qwen_probe.py` | Qwen 专用配置入口；不把厂商协议写进通用运行逻辑 |
| `events/buffered.py` | 有界后台写入与 PCM 引用预留，实时路径不等待落盘 |
| `simulator/` | 按绝对 deadline 发送、标注边界、虚拟播放与音频时间线 |
| `dataset/` / `renderers/` | 文本 corpus/schema、会话前 TTS、不可变内容缓存、半/全双工 Scenario 编译 |
| `benchmark/run.py` / `runner.py` | 单轮 latency suite、warmup/repetitions、配置/源码 hash、逐 case 工件 |
| `benchmark/evaluate.py` / `evaluator/realtime.py` | 离线有效性检查、TTFA/分母/失败计数、分组与版本化重评 |
| `benchmark/interruption.py` / `evaluator/interruption.py` | Phase 4 双轮执行、取消/播放收尾、sealed artifact 离线评价与版本化指标 |
| `benchmark/backchannel.py` / `evaluator/backchannel.py` | Phase 5 listener backchannel、false interruption 和 response continuation |
| `evaluator/duplex.py` / `scripts/prepare_duplex.py` | 显式 pause/干扰区间、负延迟保留、9 个冻结中文衍生场景、离线评价与真实 pause 工件 |
| `tools/` / `agent/` / `evaluator/agent.py` | 严格参数、call_id 幂等、固定故障、真实 Adapter 事件驱动的语音工具闭环与离线状态重放；fake 协议回归 + 一次真实 Qwen Plus 天气工具闭环 |
| `reports/html.py` | 自包含 HTML metrics report |
| `adapters/step/`, `adapters/doubao/`, `adapters/opensource/qwen_omni/` | Phase 9–11 deferred boundaries；协议未核验时明确拒绝 live use |

`EventDraft` 保留事件发生时刻；录制器添加 `seq` 和记录时间形成 `NormalizedEvent`。实时订阅者可以直接消费 draft，无需等待磁盘写入。录制 JSONL 只使用音频引用，PCM 另存。

## 文本测试集

用户可以只维护紧凑 YAML 文本集。下面的命令先补齐 Qwen TTS 缓存，再连接 Qwen Realtime；所有 TTS 在计时会话开始前完成。

```bash
.venv/bin/python -m benchmark.run \
  --model qwen-realtime --suite realtime \
  --scenario datasets/source/half_duplex/corpus.yaml \
  --tts-profile configs/tts/qwen-cherry.yaml \
  --render-missing --warmups 0
```

正式复跑时去掉 `--render-missing`，缺音频会在连接模型前失败。半双工是单次输入/单次回答；全双工目前支持在助手实际播放期间注入 interruption 或 backchannel。格式、pause segment、缓存身份和示例见 [文本测试集说明](docs/text-dataset.md)。

紧凑半双工样例已真实运行于 `runs/qwen-text-corpus-half-20260921-001/`：2/2 case pass，运行阶段 2 次 cache hit、0 次 TTS provider call，receive TTFA P50 1200.236ms、P95 1228.364ms。它只证明完整链路工作，不是模型排名结果。

紧凑全双工样例 `runs/qwen-text-corpus-full-20260921-001/` 的刺激有效且中断检测 confirmed，Stop Latency 381.465ms；新回答 completion timeout，所以 evaluator 保留为 fail。链路可运行不等于模型在该 case 上通过。

## Qwen Audio 3.0 座舱工具测试

`dataset.cockpit` 可以直接导入 `common_func_100.jsonl` 与白名单 JSONL，生成冻结 TTS、工具 catalog 和半双工 Agent suite。Audio 3.0 使用独立的 smart-turn、`longanqian` 和 2400ms 尾静音配置。

旧 5-case 工件 `runs/qwen-audio3-cockpit-smoke-20260921-001/` 的重复调用已确认由 adapter 多发首轮 `response.create` 造成，保留作历史协议证据。0.4.1 修复后的 `runs/qwen-audio3-cockpit-smoke-20260921-002/` 为3pass/1参数fail/1发送调度invalid、重复0/4；目标电量独立重试 `runs/qwen-audio3-cockpit-target-battery-retry-20260921-001/` pass且无重复。五个修复后eligible样本合计4pass/1fail、重复0/5。全量源审计另发现478条协议/期望参数冲突和140条no-tool样本；报告见 `reports/cockpit-source-audit-20260921.json`。准备命令、批量分片方式和协议实测见 [座舱 Benchmark 说明](docs/cockpit-benchmark.md)。

## Latency Benchmark

在环境中设置 `DASHSCOPE_API_KEY` 后，可直接使用已冻结的 10 条中文音频，无需重新 TTS。

```bash
uv run --locked --extra qwen python -m benchmark.run \
  --model qwen-realtime --suite realtime \
  --scenario scenarios/realtime/basic.yaml

uv run --locked python -m benchmark.evaluate --run runs/<run_directory>
```

默认一次运行 10 个 case，另加 1 个不计分的 warmup；`--limit 1` 可先跑一个用例，`--output` 指定新的输出目录，`--repetitions` 保留全部重复，`--turn-mode manual` 建立独立实验组。每次生成根目录 `metrics.json`、版本化 `evaluations/` 和每个 case 的事件、配置、输入/接收/播放 WAV、转写、trial 与哈希 manifest。HTML report 已提供自包含生成器；Phase 6 之后继续扩展报告内容。

本轮 10-case 实测为 **8 pass、1 首音频超时、1 播放调度超限 invalid**，warmup 单独排除；自动估计语音边界下，8 个有首音频的有效样本得到 receive TTFA P50 **5.15 秒**、P95 **8.85 秒**。输入只有一个 TTS 音色，边界未经人工确认，这些结果用于框架验证，不代表中文语音能力排行。

详细口径、目录和实测记录见 [Phase 3 实现说明](docs/latency-implementation.md)，资产来源与边界算法见 [datasets/README.md](datasets/README.md)。离线 evaluator 不需要 key；已验证连续重算输出逐字节一致，原始日志未改变。

Scenario 加载可仅校验 schema，也可传 `asset_root` 验证实际音频；CLI 默认检查音频。suite 中的 case 路径相对 suite YAML，音频路径相对显式 `asset_root`。`model_session_options()` 只返回公开会话选项，不包含答案、参考文本或 oracle。

## Interruption Benchmark

```bash
.venv/bin/python -m benchmark.run \
  --model qwen-realtime --suite realtime \
  --scenario scenarios/realtime/interruption_basic.yaml \
  --output runs/my-interruption --warmups 0

.venv/bin/python -m benchmark.evaluate --run runs/my-interruption
```

使用 `DASHSCOPE_API_KEY`，每次新建 output。当前一个 run 只接受一个 category，混合 suite 在运行前拒绝。未观测到停止的有效超时保留在分母，Stop Latency 为 null 并标 censored；基础设施故障和无效刺激不计分。分组按模型配置、profile、控制策略和边界方法，FIR 尚不计算。

2026-09-19 的完整 Qwen 工件为 `runs/qwen-phase4-interruption-20260919-001/`：confirmed detection，Stop Latency 371.013ms，residual 355.655ms。新回答完整播放，但先提及旧城市再澄清新城市，保守语义规则为 unknown。历史 `20260918-005` 的文本虽完整，音频播放被截断；0.3 重评将旧 pass 修正为 unknown，历史评价保留。具体边界及工件见 [Phase 4 说明](docs/interruption-implementation.md)。

## Pause / Turn Taking / Overlap

```bash
.venv/bin/python -m scripts.prepare_duplex
.venv/bin/python -m benchmark.run --model qwen-realtime --suite realtime \
  --scenario scenarios/realtime/pause_basic.yaml --output runs/my-pause --warmups 0
```

准备步骤完全离线，复用现有 TTS。另有 `turn_taking_basic.yaml` 和 `overlap_basic.yaml`，每个 category 分开运行。`input_region_start/end` 标记音频内的停顿/干扰，整句只产生一个 user_audio_end。真实 5-case pause 结果为 1 pass、3 提前响应 fail、1 调度 invalid；详细范围见 [Duplex 实现](docs/duplex-implementation.md)。

中文 Backchannel 使用 `scenarios/realtime/backchannel_tts.yaml`；旧 `backchannel_basic.yaml` 是音调 fixture，不能混为同一实验。准备/运行及 unknown 分母见 [Backchannel 实现](docs/backchannel-implementation.md)。

## Voice Agent / Mock Tools

工具闭环通过同一个 `RealtimeModelAdapter` 发送音频、接收调用和回传结果。Qwen 已按固定官方示例实现 `function_call_arguments.done` 与 `function_call_output`；已通过本地 fake WebSocket 验证，并用 Qwen Plus 完成一条真实天气工具闭环：参数为上海/2026-09-20，Mock Tool 返回多云/22℃，模型据此生成语音。工件在 `runs/qwen-agent-weather-20260920-001/`。

使用统一入口运行高级 Agent 套件：

```bash
.venv/bin/python -m benchmark.run --suite agent --model qwen-realtime \
  --config configs/qwen-agent.yaml --scenario scenarios/agent/advanced_qwen.yaml \
  --output runs/my-agent-suite --warmups 0
.venv/bin/python -m benchmark.evaluate --run runs/my-agent-suite
```

新增实际证据包括天津车次→日历创建（参数来自工具结果，最终状态核验通过）和查询执行中北京→天津改口。HTTP 500 场景中模型未重试，保留为失败。长输入20ms分帧出现背压，另建200ms分帧场景通过；两种配置单独记录，见 [Agent 实现说明](docs/agent-implementation.md)。

准备中文音频、运行及重评命令见 [Agent 实现说明](docs/agent-implementation.md)。旧 `scenarios/agent/train_single.yaml` 和 `calendar_failure.yaml` 是缺少语音资产的 oracle 模板，不能当可运行基准；CLI 会拒绝，而不会按答案执行工具。

## Qwen 真实连接验证

在运行环境设置 `DASHSCOPE_API_KEY`，准备一段 16 kHz、单声道、PCM16 WAV（最多 30 秒）。命令会真实调用 Qwen；测试套件不会。程序不自动读取 `.env`，也不接受命令行明文 key。

```bash
uv run --locked --extra qwen python -m benchmark.qwen_probe \
  --audio recordings/question.wav --output runs/qwen-manual --turn-mode manual

uv run --locked --extra qwen python -m benchmark.qwen_probe \
  --audio recordings/question.wav --output runs/qwen-vad --turn-mode server_vad
```

默认模型是已实测的 `qwen3.5-omni-flash-realtime`，音色 `Tina`。可用 `--cancel-after-chunks 1` 单独验证客户端取消，此时配置记录为 `client_forced`，不计算自然打断率。

每次运行保存 `config.json`、`session_config.json`、`input.wav`、`output_received.wav`、`raw_events.jsonl`、`events.jsonl`、`transcript.json`、`probe.json`、PCM 文件与哈希 manifest。失败也保留工件，重复运行必须使用新目录。

`output_received.wav` 是收到的 PCM 拼接，不是播放时间线；`transcript.json` 是供应商转写，不证明所有文字都已播放。当前未运行播放器，也不计算 TTFA P50/P95、Stop Latency 或 FIR。`probe.json` 的 file-end 延迟仅作连接诊断，不能替代标注 speech-end 的 TTFA；发送调度误差也会保留。

2026-09-17 真实验证结果：manual 收到 12 块音频（3.76 秒），server VAD 收到 23 块（7.12 秒），主动取消收到 `response.done.status=cancelled`。具体工件、实测协议差异与限制见 [Qwen 集成记录](docs/qwen-integration.md#8-phase-2-实现与真实验证记录)。

密钥模板见 [.env.example](.env.example)。实际 key 只通过进程环境传给 adapter；`.gitignore` 排除 `.env`、运行工件与虚拟环境。本次凭据从用户指定文件加载到临时启动进程，没有复制进项目文件。

Phase 5 Backchannel 已具备独立 runner/evaluator 和本地 sealed-artifact 回归；Phase 6 已固定 turn-taking/pause/overlap 离线指标。已有两条中文 backchannel TTS、9 个 duplex 衍生场景和一条真实 Agent 工件；仍需多样本覆盖、Overlap 内容评价、Agent 歧义追问及其他供应商接入。现有真实实验仅用于框架验证，不能代表模型总体能力；HTML report 已提供自包含生成器；Phase 6 之后继续扩展报告内容。

设计文档见 [architecture](docs/architecture.md)、[event schema](docs/event-schema.md)、[benchmark plan](docs/benchmark-plan.md)、[Qwen integration](docs/qwen-integration.md)、[provider adapters](docs/provider-adapters.md)。
