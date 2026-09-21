# Qwen Audio 3.0 智能座舱半双工 Benchmark

本文记录 `qwen-audio-3.0-realtime-flash` 的智能座舱单轮工具评测链路。输入来自文本 JSONL，经 Qwen TTS 冻结为语音；被测模型只接收 PCM 和工具 schema，不接收期望函数名或参数。

## 1. 数据源与覆盖

当前导入源：

- 工具协议：`common_func_100.jsonl`，100 个唯一非空函数，原始文件约 240KB。
- 白名单测试集：`白名单_100.jsonl`，20,169 条。其中 20,029 条有期望工具，140 条是空函数名的 no-tool 样本。
- 工具参数类型包括 string、integer、float、boolean、array 和嵌套 object。

导入器把 `input_param.<name>.define` 转成标准 JSON Schema。`choice` 转为 enum，`float` 转为 JSON Schema number，object/array 递归处理，顶层参数禁止未声明字段。源文件 hash、原始一基行号和选中记录保存在 compilation manifest。

No-tool 样本暂不混入当前 Task Completion 分母。它们需要独立的 False Tool Call Rate，不能按“缺少期望工具”直接判失败。

全量离线审计结果保存在 `reports/cockpit-source-audit-20260921.json`：20,029 条工具样本中 19,551 条符合协议 schema，478 条不符合。已发现整数/字符串类型冲突、数组被编码成字符串等问题。导入器不会静默修正；选中这些行时会在 TTS 和模型调用前失败。

## 2. Prompt 与工具执行

system prompt 参考 `qwen-audio-agent/examples/smart-cockpit` 的原则：明确操作必须真实调用工具、参数只来自用户表达、失败不得声称成功、无操作意图不得调用工具。没有复制其中绑定另一套工具名的完整后台 prompt。

`protocol_ack_v1` 是确定性测试后端：

1. 只执行模型实际产生的 `tool_call_end`。
2. 根据冻结 catalog 校验函数名、参数类型、枚举和多余字段。
3. 成功时返回固定“座舱操作已完成”，不执行真实车辆动作。
4. catalog、调用、结果和空状态均封入 case 工件，离线 evaluator 可重放。

期望函数名与参数只存在于 scenario oracle。`public_config()` 只向模型暴露 prompt 和候选工具 schema。

## 3. Audio 3.0 实测协议

参考工程确认该模型使用 16kHz PCM 输入、24kHz PCM 输出、`longanqian` 音色、`smart_turn` 和 Function Calling。实际连接进一步确认：

- endpoint 与现有 Qwen Realtime adapter 相同。
- session 必须包含 text modality；本项目请求 text + audio。
- `session.updated` 不回显 input audio format，因此该字段保留为 unverified，voice、output format 和 turn detection 仍严格检查。
- smart-turn 实际回显 2000ms 静音窗口，输入 profile 使用 2400ms 尾静音。
- 该模型在 user item 建立后需要客户端显式 `response.create`；请求携带 `tool_choice:auto`。
- 工具结果后的 response slot 存在短暂 busy 竞态，adapter 保存 raw refusal 并做 1.2s/2.6s/5s 有界重试。
- 单工具 profile 在首轮后尝试切换 `tool_choice:none`，但当前模型仍会自动重复一次相同工具调用。该行为保留为评测结果。

配置分别位于 `configs/qwen-audio-3.0-realtime-flash-agent.yaml` 和 `configs/qwen-audio3-smart-turn.yaml`。

## 4. 准备 Smoke Suite

下面选择源测试集第 254、257、384、433、1250 行，覆盖能量回收、音量、悬架、驾驶模式和充电上限。第一次执行需要 `DASHSCOPE_API_KEY`，并显式允许补齐 TTS：

```bash
.venv/bin/python -m dataset.cockpit \
  --protocol /path/to/common_func_100.jsonl \
  --testset /path/to/白名单_100.jsonl \
  --case-line 254 --case-line 257 --case-line 384 \
  --case-line 433 --case-line 1250 \
  --render-missing
```

正式运行前去掉 `--render-missing` 再执行一次。输出必须显示 `cache_hits=5`、`provider_calls=0`。当前编译 suite 为：

```text
scenarios/agent/cockpit_compiled/cockpit_audio3_flash_smoke_v1/
  0275f124dcab414abdbb/suite.yaml
```

运行命令：

```bash
.venv/bin/python -m benchmark.run \
  --suite agent --model qwen-realtime \
  --config configs/qwen-audio-3.0-realtime-flash-agent.yaml \
  --profile configs/qwen-audio3-smart-turn.yaml \
  --scenario scenarios/agent/cockpit_compiled/cockpit_audio3_flash_smoke_v1/0275f124dcab414abdbb/suite.yaml \
  --output runs/my-audio3-cockpit-smoke --warmups 0
```

## 5. 2026-09-21 实测结果

完整工件位于 `runs/qwen-audio3-cockpit-smoke-20260921-001/`。5 条均完成音频输入、Function Call、确定性工具结果回传和最终语音播放，eligible=5、invalid=0。

| 指标 | 结果 |
|---|---:|
| First Call Tool Accuracy | 5/5 = 100% |
| First Call Argument Accuracy | 5/5 = 100% |
| Redundant Identical Call Rate | 5/5 = 100% |
| Exact Tool Selection Accuracy | 0/5 |
| Exact Sequence Accuracy | 0/5 |
| Strict Task Completion Rate | 0/5 |

每条的第一次调用都与白名单函数名和参数完全一致，之后又以新 call_id 重复同一个调用一次。最终口语回复分别正确确认强能量回收、40% 多媒体音量、中等悬架、性能驾驶模式和 80% 目标电量。重复动作仍违反严格单工具 oracle，因此不能把这些 case 改判为 pass。

离线重评 ID 为 `eval_dce531cd2a086d329206`，重复评价不调用模型或工具服务。

## 6. 批量分片

先运行全量协议审计：

```bash
.venv/bin/python -m dataset.cockpit \
  --protocol /path/to/common_func_100.jsonl \
  --testset /path/to/白名单_100.jsonl \
  --audit-only --audit-output reports/cockpit-source-audit.json
```

批量准备使用一基起始行和工具样本数量。导入器会跳过空函数名的 no-tool 行：

```bash
.venv/bin/python -m dataset.cockpit \
  --protocol /path/to/common_func_100.jsonl \
  --testset /path/to/白名单_100.jsonl \
  --start-line 1 --limit 100 \
  --dataset-id cockpit_audio3_flash_shard_0001 \
  --expose-all-tools \
  --render-missing
```

`--expose-all-tools` 固定为 100 工具候选面，适合不同 shard 之间比较。默认模式只暴露所选 case 涉及的函数，适合 smoke 和调试。两种结果必须分组，不能合并准确率。

建议先处理或显式排除审计中的478条协议冲突，再冻结所有 shard 音频；随后用无 `--render-missing` 的 cache-only 编译确认并运行模型。每个 shard 使用独立 dataset_id 和 run 目录，失败与重复调用不得删除。140 条 no-tool 样本后续建立单独 suite，报告 False Tool Call Rate。
