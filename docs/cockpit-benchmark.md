# 智能座舱工具 Benchmark

本文只描述数据契约、编译方式和评价边界，不记录任何实际数据集内容或运行结果。

## 数据契约

`dataset.cockpit` 接收两份外部 JSONL：

- 工具协议定义函数名、说明和输入参数。
- case 数据定义用户文本及期望工具调用。

导入器把协议参数递归转换为严格 JSON Schema。枚举、整数、浮点数、布尔值、数组和
对象保持原始类型，顶层拒绝未声明字段。标签与 schema 冲突的行会在 TTS 或模型连接前
失败，不做静默类型转换。没有期望工具的样本应放入独立 False Tool Call Rate suite，
不能混入 Task Completion 分母。

转换目录包含缩进 JSON、JSONL、共享 prompt、工具 catalog、函数索引和带哈希的 manifest。
这些目录属于本地数据产物，不进入版本控制。

## Prompt 与 Oracle 隔离

模型公开配置只包含 system prompt 和当前候选工具 schema。`expected_calls`、期望状态、
失败计划和其他标签只供 evaluator 使用，不能进入模型上下文。

system prompt 要求模型对明确且可执行的请求调用工具，严格依据用户表达抽取参数，失败时
不得声称成功，没有操作意图时不得调用工具。

`protocol_ack_v1` 是确定性测试后端：它只执行模型实际产生的调用，按 catalog 校验函数名、
参数类型、枚举和多余字段，再返回固定结构化结果，不执行真实车辆动作。

## 候选工具拓扑

编译器支持两种必须分开报告的拓扑：

- `--expected-tool-only` 每个 case 只暴露标签工具，适合链路调试和参数抽取评价。由于函数名
  已由 schema 泄露，该拓扑不能评价多工具路由能力。
- `--expose-all-tools` 暴露完整候选目录，用于评价函数选择。工具数量、schema 总体积、响应
  延迟和超时应一起记录，不能与单工具结果合并。

若完整候选目录过大，应先用与标签无关的领域路由器生成固定候选组，再单独评价路由器和
Realtime 模型。候选组不能利用 oracle 选择。

## 编译

仅做源审计：

```bash
.venv/bin/python -m dataset.cockpit \
  --protocol /path/to/protocol.jsonl \
  --testset /path/to/cases.jsonl \
  --audit-only --audit-output /local/path/source-audit.json
```

转换并编译冻结音频 suite：

```bash
.venv/bin/python -m dataset.cockpit \
  --protocol /path/to/protocol.jsonl \
  --testset /path/to/cases.jsonl \
  --dataset-id local_cockpit_dataset \
  --tts-profile configs/tts/qwen-cherry.yaml \
  --target-model qwen-audio-3.0-realtime-flash \
  --expected-tool-only --render-missing
```

首次渲染完成后，正式运行应使用 cache-only，确保不同 target model 复用完全相同的冻结 WAV。

## Realtime 时序

Qwen smart-turn 由服务端创建首轮 response。收到 Function Calling 后，客户端发送一次
`function_call_output`，再创建一次 `tool_choice=none` 的后续 response。客户端不得为同一
user item 额外创建首轮 response，否则会污染调用序列。

Seed Duplex 3.0 使用官方 JSON Realtime 协议和 20ms PCM 分帧。输入结束或空闲时发送 mute，
新输入到来时 unmute；工具结果按 `call_id` 批量回注。

## 分片与恢复

`scripts/cockpit_campaign.py` 按一基源行窗口运行。每个窗口独立编译、运行、封口和汇总，
只在完整封口后推进 progress。恢复时会核验源转换、编译 manifest、run manifest、评价 ID
和计数，再跳过完整前缀；不重写旧报告。

cache-only campaign 可通过 `--await-cache` 等待另一个进程完成冻结音频，但自身没有 TTS
凭据，也不得调用渲染服务。`scripts/stop_campaigns_at_line.py` 只依据已封口窗口停止后台任务。

## 本地工件

运行目录、转换数据、编译 suite、进度、分片汇总和 HTML/JSON 报告全部是本地工件，受
`.gitignore` 保护。仓库只保存生成器、schema、配置和不含实际数据的自动化测试。
