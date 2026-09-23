# Phase 4 Interruption 实现与验收

当前实现包含双轮 runner、虚拟播放取消、失败路径处理和 `interruption-0.3` evaluator。
Backchannel 使用独立评价语义，见 [Backchannel 实现](backchannel-implementation.md)。

## 如何运行

设置 `DASHSCOPE_API_KEY` 后，从项目根目录执行：

```bash
.venv/bin/python -m benchmark.run \
  --model qwen-realtime --suite realtime \
  --scenario scenarios/realtime/interruption_basic.yaml \
  --output runs/my-interruption --warmups 0

.venv/bin/python -m benchmark.evaluate --run runs/my-interruption
```

每次使用新的 output 目录。离线评价无需 key，重复执行不调用模型。`benchmark.interruption_evaluate` 委托同一个评价入口。一个 run 当前只接受一种 category；混合 latency/interruption 会在创建目录、构造 adapter 之前拒绝。

工件布局与 latency 一致：根目录 config/manifest/metrics，`cases/<scenario_id>/<attempt_id>/` 下保存冻结场景、配置确认、input/output/output_received WAV、raw/normalized JSONL、transcript、trial、PCM/blob 和哈希 manifest。`evaluations/<evaluation_id>/` 保存评价配置和结果；旧评价及原始事件不改写，根目录 metrics 是最新结果的便捷导出。

## 执行与观测

- 接收、输入发送、播放、持久化并行。第一轮响应触发后停止追加的 t1 尾部静音，等待当前输入发送结束，再发送 t2；不会把两轮 PCM 交错在同一输入流。
- `where`、`occurrence`、trigger timeout 均执行。delay 相对命中事件的 monotonic 时间；错过 deadline 超过 profile 门槛记 invalid。`scenario_action_start` 保存命中的因果事件，`interrupt_start` 对齐第二段实际标注语音开始，前导静音不计入刺激。
- `response_still_generating` 只证明已见 response start、尚未见 response end，不能解释为服务端 GPU 仍在计算。`response_still_playing` 要有未停止的虚拟播放及未消费 PCM。
- `minimum_continuation_evidence.remaining_ms` 使用已收到、尚未播放的本地 PCM，包括正在消费 quantum 的剩余部分。在发送前和实际语音开始处检查，并从封口事件离线复算。未达到预登记阈值的刺激不计分；不再用“已播放时长”替代，也不再仅加 unverified 注释后放行。
- 旧 response end、新 response start/end 使用同一个 speech-end 后观察期限。新 response 必须关联 t2 且发生在 stimulus 后。断线/collector failure 立即唤醒等待；不继续等待到模型超时后归错因。
- case 观测和播放 drain 分开计时；`max_case_duration_ms` 约束观测，close/drain 另外有界。`scenario_action_end` 标记进入清理前的观测终点，清理造成的停止不算模型停止。

播放器在下一个 quantum 边界响应取消，默认 quantum 为 20ms。已播放区间写入 `assistant_playback_chunk`，未消费的当前 chunk 尾部、队列中的 chunk 和迟到 chunk 均有 `audio_chunk_dropped`。同一 response 最多一次 playback stop；取消前没播放过则没有虚构的 audible stop。

正常 audio end、server cancel 和 `case_abort` 的停止原因分别保存。drain 超时完成当前 quantum 后丢弃剩余 PCM，并保留完整接收音频。截断新回答会使 spoken context switch 为 unknown，不能仅凭完整供应商文本判 pass。

## Qwen 顺序差异

协议允许 `response.done(status=cancelled, reason=turn_detected)` 与 `speech_started` 乱序到达。
mapper 在同一最新 response、无客户端 cancel 请求、明确 turn reason、后到 VAD 在一秒内且
没有新 active response 时补齐检测证据。这一秒是本地保守关联窗口，不是厂商 SLA。

VAD 先到时同样要求服务端取消证据。client cancel、未知客户端/服务端竞态、过旧 cancel 和无关 reason 不生成 confirmed detection。input item 通过唯一未提交输入回合关联；已提交 t1 的尾部静音不会把 t1 再次加入 t2 的响应队列。

## 评价口径

| 输出 | 当前定义 |
|---|---|
| eligible | 封口完整、输入刺激完整、前置条件成立、无客户端控制污染、时间误差在 profile 内 |
| Interruption Detection Rate | confirmed 服务端取消与 VAD 证据的 case 数 / eligible 数；不宣称语义级 interruption/backchannel 区分能力 |
| Stop Latency | 观测窗内旧 response 的实际虚拟播放停止减 `interrupt_start`；`case_abort` 排除 |
| censored | 观察期没有模型播放停止：latency 为 null、censored_count 单列，保留有效超时分母 |
| Residual Audio | 对旧 response 已播放样本区间与 stimulus→stop 窗求交；缺少 stop 时仅报观察期下界，不进入完整 residual 分布 |
| Context Switch | 完整新 response、有音频、收到的音频全部播放后，对 spoken transcript 做新意图/旧意图规则检查 |
| context unknown | 文本规则无法判定、转写缺失或回答被截断；accuracy 为 null 或以已判定样本为分母，并显式输出 known/unknown 数 |
| Case Success Rate | pass / eligible；attempted、invalid、infra_failed、unsupported、unknown、warmup 另列 |

发送迟到、发送耗时、播放 quantum 迟到均离线复核。均值及 P50/P90/P95/P99 使用有观测值的非负样本，并同时给 n、eligible、censored 数。FIR 在 interruption suite 中为 null，留待 Backchannel。

分组保留完整模型配置、控制策略、measurement profile、播放模式、边界方法/状态/分辨率。逐 WAV 的 RMS threshold 留在场景工件，不再把同一 suite 切成无意义的单样本组。自动边界仍不与人工边界合并。

## 发布边界

真实输入、运行目录、逐 case 证据和评价结果均保存在本地工件，不写入仓库文档。
`transcript.json` 是厂商转写，不能替代独立音频内容核验；虚拟播放也不等同于真实扬声器测量。
