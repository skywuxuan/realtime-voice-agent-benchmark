# Phase 7/8 Mock Tools 与 Voice Agent

已实现由 Adapter 工具事件驱动的音频 Agent 运行路径、Qwen Audio 3.0 工具协议映射、
确定性工具运行时及封口工件离线重评。

## 纠正旧实现

旧 `agent.run` 和 `agent.runtime.run_fixture` 直接循环 `expected_calls`。这只是执行标准答案，无法评测 Agent 选择工具的能力；旧的“Agent 已完成”表述不成立。本版本删除这条执行路径，`expected_calls`、`expected_final_state` 和 `forbidden_calls` 仅由 evaluator 使用。没有音频的旧 `train_single.yaml`、`calendar_failure.yaml` 是 oracle 模板，CLI 会在联网前拒绝它们，不自动补造模型调用。

数据库初始状态恰好等于预期状态，也不能令查询任务通过。`agent-0.3` 要求模型确实发起成功的正确调用、参数及序列符合 oracle、没有禁止调用，且显式预期最终状态一致。

## 运行路径

```text
冻结中文音频 → RealtimeModelAdapter → tool_call_end
  → 等待对应 response 完成 → 参数校验 / Mock Tool 执行
  → tool_execution_start/end + tool_result → Adapter.send_tool_result
  → 下一 response 的流式音频 → 虚拟播放 → 封口工件
  → 离线状态重放 / 参数与序列 / Task Completion
```

`agent.runtime.public_config()` 只将公开 system prompt、固定业务时间和已启用工具定义交给 adapter。答案、期望状态、故障计划不进入模型配置。输入逐个冻结 WAV 发送；`turn_triggers` 可以等待前一回答播放结束，或绑定前一轮某工具的 execution_start，在其执行期间发送改口音频。语音开始时工具仍在等待的证据由 evaluator 离线复核，缺失则 invalid。

每个 attempt 新建 MockToolServer，载入 initial_state。接收任务持续记录音频和事件，工具结果由另一执行路径回传。`tool_delays` 在 tool_execution_start 后施加固定异步等待，音频收发继续运行；执行本身按 FIFO 串行化，故障调用次数可重放。Timeout/HTTP 500 等仍为预登记模拟故障，不冒充真实网络异常或超时后已提交事务。

## 工具与故障

| 工具 | 参数与行为 |
|---|---|
| weather | city、YYYY-MM-DD date；固定城市天气 |
| train | from_city、to_city、date；查询固定列车表，空结果仍保存 |
| calendar | list/create/update/delete；写入校验 title、start/end 顺序和 event_id |

额外参数、非法日期、结束早于开始、未启用工具均返回结构化错误。故障按工具名与第几次调用固定匹配，支持 timeout、http_500、no_result、permission_denied、invalid_argument。`no_result` 为成功的空查询结果，其他故障不会偷偷修改最终状态。

同一 call_id、相同参数重复投递返回缓存结果，不重复写库、不再次注入故障、不重复生成执行事件；同一 call_id 携带不同参数明确拒绝。返回值和 state snapshot 采用副本，调用方不能借引用修改数据库。

## Qwen 协议依据

实现依据为固定的 [Audio 3.0 Function Calling 客户端](https://github.com/aliyun/alibabacloud-bailian-speech-demo/blob/1082942345c555429ee61c04b8c585f12e06dab2/samples/conversation/fun-audiochat-realtime/fun_realtime/client.py) 和 [Smart Cockpit runner](https://github.com/QwenAudio/qwen-audio-agent/blob/149440d01d5f3ff4feaf2c6f904d05e6ac81d499/examples/smart-cockpit/bench/runner/run-realtime.mjs)。

- 会话工具定义转换为示例的 `tools: [{type: function, function: ...}]`。
- `response.function_call_arguments.done` 归一化为 tool start/arguments/end。同一原始消息的三项事件共享观察时间，不计算虚假的参数生成耗时。
- 非对象/损坏 JSON 标为 invalid，绝不执行部分参数。
- 收到完成的工具 response 后，逐个 `conversation.item.create(function_call_output)` 回传结构化结果。
- 同一 response 的全部 tool results 送达后才发送一次 `response.create`；结果重复投递不重复启动回答。新 response 保留原 user turn 的关联。若已开始发送新的用户回合，旧工具结果仍注入历史，但不再额外 create 一条旧问题回答；新回合由正常输入/VAD 产生。该 continuation policy 与 superseded 标记写入 tool_result_sent 的 vendor 诊断字段；不依据 oracle 改参数或取消工具。

配置前工具能力为 `verification=docs`；观察到实际 `tool_call_end` 后，tool_calling 可标为
当前会话 experiment。不同模型仍需独立能力证据。

## 使用

仅工具与协议单测不需要 key。真实运行需要先准备语音资产，此步骤会调用已授权的 Qwen TTS：

```bash
uv sync --locked --python 3.11 --extra qwen
# 已有冻结资产可直接用；以下准备步骤仅首次需要，会调用 TTS
.venv/bin/python -m scripts.prepare_agent --limit 1 --transport stdlib
.venv/bin/python -m scripts.prepare_agent_advanced --transport stdlib

.venv/bin/python -m benchmark.run --suite agent --model qwen-realtime \
  --config configs/qwen-audio-3.0-realtime-flash-agent.yaml \
  --scenario scenarios/agent/advanced_qwen.yaml \
  --output runs/my-voice-agent --warmups 0
.venv/bin/python -m benchmark.evaluate --run runs/my-voice-agent
```

上述 WAV/YAML 由准备命令生成。stdlib 路线不需要 assets extra；SDK 路线仍可选。
key 只由 `DASHSCOPE_API_KEY` 读取。传给 runner 的输入必须是冻结且 SHA-256 一致的 WAV。

新命令支持单场景和 suite，统一保存到 `cases/<scenario_id>/<attempt_id>/`，run 根有索引、metrics 和 report；旧单场景根目录录制仍支持重评。每个 case 工件包含 config、scenario、session_config、raw_events、events、input/output/output_received WAV、transcript、tool_calls.json、tool_results.json、state.json、trial 与哈希 manifest。tool 调用、执行、结果也在统一 events.jsonl 内。

大批量 suite 可启用 `--artifact-mode compact`。runner 先按普通目录完成封口和评价，再生成
规整的 `results.json`、示例会话配置和 `evidence.tar.gz`。归档逐文件核对内容树哈希，重复
内容使用 tar hard link，只在深度验证通过后删除松散 `cases/`。`results.json` 直接保存
`user_audio_end` 到最终 `tool_call_end`、首个 `assistant_audio_start` 的延时和证据时间戳。
冻结输入音频仍由场景中的内容寻址路径引用，不进入清理范围。需要重新离线评价时先执行
`python -m agent.artifacts restore <run>`；恢复后的文件哈希必须与原 case 树一致。

离线评价先校验封口哈希，再核对每条执行都来自对应模型 `tool_call_end`，从 initial_state 按故障计划重放调用，验证每个 ToolResult、最终状态和 trace。修改 evaluator 时生成新的 `evaluations/<evaluation_id>/`，不修改原始工件。

## 指标边界与剩余工作

Task Completion 同时检查成功调用和最终状态；不能仅用 unchanged-state 或“已经完成”文本判成功。重试只在同一工具、同一参数之后确实成功时算 recovery；其他随意调用不算恢复。额外成功调用会影响序列匹配。没有故障时 recovery 为 null，没有 correction 标签时 correction 指标为 null。开放中文完成声明尚无校准提取器，保存 `completion_claim_status=unknown` 和 `hallucinated_action=null`；离线测试可显式提供已标注声明验证指标。

已实现有序步骤的结果引用和依赖时间证据、read-only 查询执行中 Correction。剩余内容包括
允许多种拓扑顺序的 DAG、可逆写入后的补偿、Ambiguous Request 的追问证据、
timeout-after-commit 故障和自然语言完成声明抽取。

## 高级场景

`expected_calls` 中可声明 `step_id`、`depends_on`，参数可使用 `{"$result":{"step":"search","path":["trains",0,"depart_at"]}}` 引用实际成功调用的结果。引用只能指向前序步骤；除参数相符外，还要证明前一步 tool_result_sent 不晚于后一步 tool_call_start，提前猜出正确参数也不能通过依赖验证。这些规则只存在于 evaluator。

`argument_comparison=typed_iso8601` 明确启用 schema 校验、忽略可选 null、按固定场景时区规范化等价 ISO 时间；不修改工具实际参数和数据库。旧场景默认 exact，不能静默放宽原有标准。

不同分帧配置必须使用独立 scenario ID、tag 和工件，不能合并结果。运行时不会按
`expected_calls` 补做工具；HTML 报告只从封口工件生成，并保持在本地。
