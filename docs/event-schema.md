# Unified Event Schema 与 Scenario Schema

状态：**v0.1 契约、Qwen 映射、Phase 3 单轮 latency 执行/虚拟播放/离线指标已实现；通用打断场景待后续阶段**。日期：2026-09-17。

所有字段均为本项目统一契约，不是任何厂商 API 的事件定义。厂商字段映射由 adapter 负责，见 [Qwen 集成](qwen-integration.md)。指标只使用本文件定义的时间域和关联关系，公式见 [benchmark plan](benchmark-plan.md)。

## 1. 事件 envelope

```json
{
  "schema_version": "0.1",
  "event_id": "ev_000042",
  "seq": 42,
  "run_id": "run_20260917_a1b2",
  "scenario_id": "zh_latency_001",
  "attempt_id": "attempt_001",
  "session_id": "session_local_001",
  "clock_id": "process_001",
  "timestamp_monotonic_ns": 123456789000,
  "wall_clock_timestamp": "2026-09-17T09:00:00.123456Z",
  "recorded_monotonic_ns": 123456800000,
  "source": "assistant",
  "producer": "adapter.qwen",
  "event": "assistant_audio_chunk",
  "turn_id": "turn_001",
  "response_id": "response_001",
  "item_id": "item_001",
  "call_id": null,
  "stream_id": "output_response_001",
  "causal_event_id": "ev_000040",
  "raw_event_ref": "raw_000031",
  "timing": {
    "basis": "client_receive",
    "uncertainty_ns": 1000000
  },
  "payload": {
    "chunk_index": 0,
    "audio_ref": {
      "path": "audio/received/response_001.pcm",
      "encoding": "pcm_s16le",
      "sample_rate_hz": 24000,
      "channels": 1,
      "byte_offset": 0,
      "byte_length": 960,
      "sample_offset": 0,
      "sample_count": 480
    },
    "late_after_cancel": false
  }
}
```

这个例子只展示字段关系，不代表真实运行数据；真实 ID 必须在其约定范围内唯一。

| 字段 | 约束 |
|---|---|
| `schema_version` | major/minor 契约版本，写入每一行 |
| `event_id` / `seq` | event_id 在 run 内唯一；seq 在 case attempt 内严格递增，由单一记录入口分配 |
| `run_id` | 一次命令执行的标识，可含多个 case |
| `scenario_id` | 数据集稳定唯一标识，不含模型名；语义改变增加 scenario_version |
| `attempt_id` | 每次重试/重复都不同，不覆盖旧 attempt |
| `session_id` | 本地稳定 ID；vendor_session_id 放 session_start payload |
| `clock_id` | 同一单调时钟域的标识；跨进程/机器禁止直接相减 |
| `timestamp_monotonic_ns` | 事件观测或明确的样本边界发生时间；metric 主时钟 |
| `wall_clock_timestamp` | 同步采集的 UTC RFC3339 时间，只用于定位/审计 |
| `recorded_monotonic_ns` | 记录器接收时刻，用于诊断 queue delay，不作为网络/模型 latency 起点 |
| `source` | 语义角色：user/assistant/system/tool；不是负责写日志的组件名 |
| `producer` | runner/simulator/adapter.qwen/playback/tool_runtime 等实际来源 |
| `turn_id` / `response_id` | 音频所属用户回合及助手响应；不允许靠“最近一条 response”盲配 |
| `item_id` / `call_id` / `stream_id` | 文本/音频 item、工具调用、音频流关联；不适用时 null |
| `causal_event_id` | 直接触发证据；复杂多因果可在 payload 增加 evidence_event_ids |
| `raw_event_ref` | 可追溯至原始协议日志；本地事件可为 null |
| `timing.basis` | client_receive / client_send / simulator_boundary / virtual_playback / device_playback / inferred |
| `timing.uncertainty_ns` | 已知边界量化误差，未知为 null；不是伪造统计置信区间 |
| `payload` | 按 event 判别的结构化对象；vendor 私有字段只放 vendor 扩展区 |

`timestamp_monotonic_ns` 是唯一规范字段，不另存含义不明的 `timestamp` 或重复的 `monotonic_timestamp`。HTML/JavaScript 读取时按十进制字符串或 BigInt 解析纳秒，避免超过 2^53 后丢精度；显示使用相对 session 原点的毫秒。

## 2. 时间语义

### 接收、发送、播放分离

- 入站厂商消息到达 callback/transport 边界时立即记录 monotonic 与 wall clock，再解析、解码、排队。音频解码失败不能产生成功 audio_start。
- 出站帧记录发送开始、完成回执和计划 deadline；`user_audio_chunk` 描述本地实际发送，无法证明远端收到的时刻。
- `assistant_audio_start` 表示该响应首个有效非空 PCM chunk 在客户端收到。它可以包含前导静音；若另测首个语音样本，必须用独立 VAD/alignment 派生指标。
- `assistant_playback_start/stop` 表示样本被播放器消费/停止。MVP 使用 paced virtual sink，timestamp 是本地虚拟播放时钟；后续设备测量必须另标 profile，不能混合汇总。
- 厂商 VAD 的 `audio_start_ms/audio_end_ms` 等若存在，是服务端音频时间轴，不自动等同于本地 monotonic；保存在 payload，映射需提供样本索引及误差。

`user_audio_end` 定义为该用户语音段最后一个标注语音样本在客户端输入时间轴上结束的时刻。尾部静音继续上传，但不推迟这个边界。源资产需要人工检查的 speech_bounds；没有可靠标注时只能报 segment_end 代理指标，并标明不可与 speech_end TTFA 直接比较。

文件中间的 pause 是同一用户回合内部的静音，不产生新的 user_audio_end。按样本边界生成的事件允许稍晚写入日志；seq 表示记录顺序，timestamp 表示发生时间，二者不要求完全同序。算法用 causal IDs 和时钟域排序，不能仅凭 JSONL 行次相减。

每 session 保存 monotonic 原点、wall clock 原点、时钟分辨率与进程 ID。多机器扩展先把边界都落到 runner 接入侧；未测出的时钟偏移不能通过 wall clock 拼成精确 latency。

## 3. 核心事件与附加事件

### 用户要求的事件

| event | producer / 触发条件 | 最小 payload |
|---|---|---|
| `session_start` | adapter，连接并获得有效 session | vendor_session_id、adapter_version、capabilities |
| `session_end` | runner，close/失败收尾，仅一次 | reason、complete、last_response_ids |
| `user_audio_start` | simulator，标注语音段开始 | action_id、asset_id、sample_index、annotation_source |
| `user_audio_chunk` | simulator，帧发送完成 | audio_ref、chunk_index、planned_send_ns、send_started_ns、send_completed_ns、silence |
| `user_audio_end` | simulator，标注语音段结束 | action_id、end_sample、annotation_source |
| `user_turn_commit` | simulator，manual 控制请求与提交完成 | phase=requested/submitted，envelope 关联 turn_id |
| `vad_start` / `vad_end` | adapter/独立 VAD，真实检测事件 | detector、vendor_item_id、vendor_audio_offset_ms（可空） |
| `assistant_response_start` | adapter，响应开始证据 | response_status、trigger_turn_id、association_method |
| `assistant_audio_start` | adapter，该响应首个有效 PCM | first_chunk_event_id、audio_format |
| `assistant_audio_chunk` | adapter，每块解码成功音频 | audio_ref、chunk_index、late_after_cancel |
| `assistant_audio_end` | adapter，明确结束/取消/流异常 | reason、last_chunk_event_id、complete、completion_source |
| `assistant_text_delta` | adapter，文本/语音转写增量 | text、channel、delta_index |
| `assistant_text_done` | adapter，最终文本或可溯源拼接 | text、channel、completion_source |
| `interrupt_start` | simulator，设计为打断的音频开始且旧响应在播 | action_id、target_response_id、intent_revision |
| `interrupt_detected` | adapter，明确且可关联的停止/打断证据 | target_response_id、mechanism、evidence_event_ids、evidence_level |
| `assistant_cancelled` | adapter，收到实际取消确认 | target_response_id、initiator、reason、evidence |
| `tool_call_start` | adapter，首次观察到调用 | name、call_id、response_id |
| `tool_call_arguments` | adapter，参数增量/完整参数 | representation: delta/final、text、parsed_arguments、parse_status |
| `tool_call_end` | adapter，调用输出完整 | name、arguments、valid_json、completion_source |
| `tool_result` | tool runtime，执行成功或结构化失败 | call_id、status、result/error、state_hash、execution_id |
| `error` | 任意组件，故障 | category、code、message_redacted、fatal、scope、retryable |

`tool_call_end` 仅表示模型已输出完整调用，不表示工具已经执行。raw 只有完整调用时，可以在同一接收时间派生 start/arguments/end，并写 `completion_source=single_message`；不得据此报告工具参数生成耗时。

文本 channel 至少区分 `spoken_transcript` 与 `text_response`。同一内容出现两种流时不可重复拼接。供应商输出转写、输入识别与离线 ASR 分别存储来源；任何文本 delta 都不证明对应语音已经播放。

### 为可靠测量补充的事件

| event | 用途 |
|---|---|
| `session_configured` | requested/effective config 及服务端确认依据 |
| `scenario_action_start/end` | action 触发条件、绑定 response、实际时间、timed_out |
| `user_text_done` | 厂商输入转写，关联输入 item，不能替代参考文本 |
| `assistant_response_end` | completed/cancelled/failed/unknown；区分音频结束和整个响应结束 |
| `assistant_playback_start/chunk/stop` | 每个 response 的 sample offset、sample count/rate；chunk 含 audio_ref、planned_end_ns、wake_lateness_ns；stop 含 stop_reason |
| `playback_buffer_cleared` | initiator、policy、response_id、丢弃样本数、触发事件 |
| `audio_chunk_dropped` | 迟到/取消/溢出等具体原因，仍保留原始音频 |
| `interrupt_requested` | 记录主动调用 adapter.interrupt，不能当检测结果 |
| `backchannel_start/end` | 仅用于场景标注与评价，不能触发隐藏 cancel |
| `tool_execution_start/end` | 执行区间、确定性 failure fixture、状态版本 |
| `tool_result_sent` | adapter 结果注入回执，与实际执行结果分离 |
| `case_end` | completed/model_failed/infra_failed/invalid/unsupported 与原因 |

MVP 核心事件 schema 可声明工具相关类型，但无需实现 tool runtime。未知厂商事件先保存 raw；只有明确语义才能映射为核心事件。若 session 意外关闭，只能生成带 `complete=false` 的 inferred end，不能伪造正常结束或取消确认。

## 4. 打断证据与关联规则

`vad_start` 只说明检测到语音，不能独自推出“识别了 interruption”。`interrupt_start` 是评测刺激标签，模型看不到；backchannel 也是用户音频，但不会产生 interrupt_start。

将证据分层存储：speech detection、response cancellation、playback stop、intent update 分开。自然打断 profile 不按刺激标签发送 `response.cancel` 或清空队列；只允许根据已观察到的通用协议证据执行预先配置的播放器策略。若 profile 定义任意 VAD 即清队列，这是明确的 client_vad_flush track，其结果与 native_server track 分开。

取消确认未暴露时，可以从同一旧 response 的音频消失/恢复区间得出 `inferred` 行为结果，必须引用观测窗、未发生 underrun 的证据及 eligible 条件；不能补造 `assistant_cancelled`。证据不足时检测率/误打断率给区间及 unknown 数，规则详见 benchmark plan。

响应关联优先使用 vendor response/item/call IDs 与输入提交/VAD item。provider 不返回关联字段时，在“单个未决 user turn”前提下做 `association_method=inferred_serial_turn`；多轮重叠无法唯一关联则标 `ambiguous`，相关 latency 不计入有效分位数。禁止把打断后的新音频当作旧音频的 continuation。

并发工具调用按 call_id 汇聚参数，每个 completed call 仅执行一次。重复 raw 事件保存且标 duplicate，不能触发重复工具副作用。客户端请求 cancel 与服务端确认、以及工具执行取消互不等价。

## 5. Raw event 格式与完整性

raw envelope 至少包含 `raw_event_id`、run/scenario/attempt/session、`direction=sent/received`、`timestamp_monotonic_ns`、wall clock、`transport`、`vendor_event_type`、`body` 或 `body_ref`、redacted_fields、blob hash。认证头不进入日志。

大音频可以无损外置，normalized audio_ref 指向解码后的 PCM，raw body_ref 保留原始 payload。manifest 覆盖所有文件 hash。录制中断时保留 partial 文件、最后有效 seq 与错误；离线评估拒绝把截断 JSONL 当完整会话。

评价产物不回写原始 events.jsonl。推断 stop、对齐 ASR、rule/judge 判定写在 `evaluations/<evaluation_id>/`，保留源 event IDs、算法版本和推断不确定性。这样可以改善 evaluator 而不改写历史证据。

## 6. Scenario Schema

场景采用 YAML，启动前转为规范化 JSON 校验并 hash。以下为示意，音频路径和 hash 尚未创建；此块不能冒充可运行 fixture。

Phase 3 每个音频资产另有 `boundary_annotation`，包括 method、status、resolution_ms、parameters。旧场景缺失时默认为 unverified，不能直接产生 speech-end TTFA。当前 10 个真实 TTS fixture 为 `energy_rms_v1 / automatic`，人工边界和自动边界分组；resolution 不是语义误差置信区间。可运行场景见 [basic.yaml](../scenarios/realtime/basic.yaml)。

```yaml
schema_version: '0.1'
scenario_id: zh_interruption_001
scenario_version: 1
suite: realtime
category: interruption
language: zh-CN
tags: [human_or_frozen_tts, clean, intent_correction]
seed: 17
world:
  now: '2026-09-19T10:00:00+08:00'
  timezone: Asia/Shanghai
capabilities_required: [audio_input, audio_output, streaming_output]
session:
  system_prompt: 请用自然简洁的中文交流。
  turn_mode: server_vad
  control_profile: native_server
audio:
  input_encoding: pcm_s16le
  channels: 1
  chunk_ms: 20
  assets:
    question:
      path: datasets/audio/zh_interruption_001/question.wav
      sha256: '<由资产准备阶段填入的64位hash>'
      reference_text: 请介绍北京几个适合周末游览的地方。
      speech_bounds_samples: [0, 64000]
      sample_rate_hz: 16000
      provenance: {kind: frozen_tts, speaker_id: speaker_01}
    correction:
      path: datasets/audio/zh_interruption_001/correction.wav
      sha256: '<由资产准备阶段填入的64位hash>'
      reference_text: 等等，换成上海呢？
      speech_bounds_samples: [0, 28800]
      sample_rate_hz: 16000
      provenance: {kind: frozen_tts, speaker_id: speaker_01}
actions:
  - action_id: ask
    type: play_audio
    asset: question
    turn_id: t1
    trigger: {type: session_ready}
  - action_id: correct
    type: play_audio
    asset: correction
    turn_id: t2
    stimulus: interruption
    intent_revision: 2
    trigger:
      type: after_event
      event: assistant_playback_start
      where: {turn_id: t1}
      occurrence: 1
      bind: {target_response_id: response_id}
      delay_ms: 800
      timeout_ms: 15000
    preconditions:
      - {type: response_still_playing, response: '$target_response_id'}
      - {type: response_still_generating, response: '$target_response_id'}
      - {type: minimum_continuation_evidence, remaining_ms: 800}
oracle:
  hidden_from_model: true
  stimulus: interruption
  expected_new_intent: {city: 上海}
  metric_profile: realtime_v0_1
  assertions:
    - {type: old_response_stops}
    - {type: answer_targets_city, city: 上海}
termination:
  max_case_duration_ms: 60000
  response_timeout_ms: 15000
  post_stimulus_observation_ms: 5000
  drain_timeout_ms: 5000
```

方案默认 20 ms 帧，属于工程起点而非官方 API 强制要求；Phase 2 验证服务端限制再固定。采样率以 adapter 协商为准，若重采样，原始/发送音频均保存，并映射 speech bounds。

### 场景执行约束

1. `session`、音频内容和未来 tools schema 可以发给模型；`oracle`、reference_text、expected_tool_calls、expected_state、刺激标签仅供 simulator/evaluator 使用，不作为额外文本提示泄露给模型。
2. trigger 可为 session_ready、绝对 offset、after_event；必须有 occurrence、关联过滤、超时，绑定后不随“最新响应”变化。缺少触发事件不是模型“打断成功”。
3. delay 相对被绑定事件的 monotonic 时间。simulator 逐帧按绝对 deadline 调度，保存实际偏差；不能累计 `sleep(chunk_ms)` 漂移，也不能落后后无记录地 burst 补发。
4. `minimum_continuation_evidence` 使用当时缓冲可播放长度或预先登记的对照依据，不能看完结果后选择有利样本。若证据不足，不声称自然结束是被打断，标 invalid/ambiguous 并保留计数。
5. Backchannel 将第二段标为 `backchannel`，其 trigger 与前置条件一致；它仍发正常 PCM，不发送 interrupt 命令。观察窗内有 client flush、网络 underflow 或自然结束歧义时分别记因。
6. Latency 场景只有一次提问及等待，manual/server_vad 分 track。不要通过缩短尾部静音改变 user_audio_end 定义。
7. Pause 在 asset 内含标注的静音段，附 `pause_windows`，200/500/800/1200/2000 ms 同文本变体。Overlap 使用音轨/变换描述，记录目标说话人、旁人/背景类别与混音区间。
8. 一个 suite YAML 可含 case 路径列表与 repetitions；每 case 仍有唯一 scenario_id，重复次数使用 attempt_id，不复制 ID。

### 后续 Agent 扩展

Agent scenario 加 `tools.enabled`、fixture version/hash、initial_state、failure_schedule、固定日期、允许重试与依赖约束。oracle 支持 exact typed args、required/forbidden calls、ordered calls 或 dependency DAG、expected final state、禁止的写入，以及 result-backed answer assertions。

`intent_revision` 仅是评价标注；工具结果不得被 benchmark 根据该标签自动修正为正确答案。模型必须在实际听见 correction 后更改调用或按规则补偿。fault schedule 按 tool name + match args + invocation index 决定，不能依赖墙钟随机注入。

## 7. 实现状态与验证边界

- 已实现：序列化往返、版本/额外字段拒绝、ID/引用完整性、音频 byte/sample 长度一致。
- 已实现：观测与记录时间分离、跨 clock_id 相减拒绝、wall clock 跳变不改变单调钟间隔。
- 已实现：manual/server VAD 提交约束、单接收者、有界队列完整分发、连接中关闭的竞态保护。
- 已实现：raw/normalized/PCM/blob 保存、凭据字段及显式 secret 值脱敏、hash manifest、失败/取消时保留 partial artifact、默认拒绝将 partial 当完整录制。
- 已实现：MVP 三类场景和 suite schema、显式 stimulus、触发器/前置条件结构、固定业务时间、WAV/hash/speech bounds、suite 唯一 ID 校验，以及 oracle 与公开 session options 的边界。
- Phase 2 已实现：Qwen response/audio/item 的单输入回合关联、原始事件去重对应的 normalized 去重、音频晚到标记、配置请求/回显/未确认参数、主动取消确认；关闭后可排空事件至 EOF。
- Phase 3 已实现：单个 session_ready latency action、在 speech bounds 处拆帧、speech-end → 实际音频提交块的因果引用、有界后台写入、虚拟播放样本区间、计时门槛、TTFA/超时/提前响应/分母的离线重评。
- Phase 4 已实现：after_event 的 where/occurrence/deadline、语音开始处的刺激标记与缓冲前置条件、双轮输入引用、server cancel 与 client cancel 区分、迟到/部分 PCM 丢弃、断线/超时/缺失结束的工件回归。`scenario_action_end` 标记观测终点；`assistant_playback_stop.stop_reason=case_abort` 不作为模型停止。详见 [实现说明](interruption-implementation.md)。
- Phase 5/6/7/8 已加入：backchannel_start/end、显式 input_region_start/end、duplex gap/pause/overlap 离线测量、tool execution/result 审计和 deterministic state hash。复杂重叠 action、多供应商 function-call 适配和中文语义 judge 仍待扩展。

实现入口为 [events/schema.py](../events/schema.py)、[recorder.py](../events/recorder.py)、[replay.py](../events/replay.py)、[scenarios/schema.py](../scenarios/schema.py) 与 [loader.py](../scenarios/loader.py)。`EventDraft` 不含 seq/recorded time，录制器添加后形成本文 envelope 的 `NormalizedEvent`。Payload 按 event 在 Python validator 内分型；不将仅调用 `model_json_schema()` 的结果宣称为完整的跨语言事件校验器。

`read_recording()` 默认要求完整 manifest 与文件哈希。中断检查需显式 `allow_partial=True`；未封口 JSONL 只读取完整行，拒绝中间损坏的行。`complete` 表示工件结构完整，不代表模型任务成功。未知厂商事件先存 RawEvent，不放宽 normalized event 类型。

Phase 1 的 Scenario 执行范围仅为结构与资产校验，`model_session_options()` 只返回公开字段。Pause/overlap 使用 AudioAsset.regions 标注及同名区域的输入边界事件，整句仍只产生一个 user_audio_end；Agent scenario 使用独立 `tools.scenarios.AgentScenario`，避免把工具 oracle 泄露给模型。当前未知字段会明确拒绝，不悄悄忽略。

离线可运行示例和验证命令见 [README](../README.md)。合成音调 fixture 不提供模型指标，也不能验证真实 interruption/backchannel 行为。
