# Phase 3 Latency 实现

日期：2026-09-17（Phase 3 历史验收）。当前支持 Qwen Realtime、单轮 latency 场景、虚拟播放和离线评价。Phase 4/5/6 进展分别见 interruption、backchannel 和 duplex 说明；HTML report 已提供基础生成器。

## 执行路径

```text
冻结 Scenario + WAV + 音频 hash + 边界标注
  → benchmark.run（默认并发 1，每 case 独立会话）
  → simulator.input（按 monotonic deadline 发送，20 ms 帧）
  → RealtimeModelAdapter
  → 接收时间戳 + PCM 内存引用
  → simulator.playback（20 ms 虚拟消费区间）
  → BufferedArtifacts → EventRecorder（独立后台写入）
  → 封口后的工件 → benchmark.evaluate → metrics.json
```

厂商解析仍在 adapter 内。`adapters/registry.py` 负责选择 adapter 和凭据环境变量；通用 runner、播放器及 evaluator 不检查厂商事件名。数据准备的 DashScope TTS SDK 不进入 benchmark 计时路径。

`BufferedArtifacts` 对 PCM 引用预留 offset，并将 raw、PCM、normalized event 写入同一个 FIFO。提交立即返回，磁盘写入由后台任务完成。结束前必须等待写入完成，并检查预留引用与实际文件一致。队列同时限制任务数与字节数，PCM 内存也有上限；溢出和写入错误使结果无效，不丢掉事件后继续记成功。

## 时间边界

输入会在标注的 speech start/end 样本处拆帧。`user_audio_end` 绑定最后一个含语音的输入块，时间为该块本地 transport 提交完成的 monotonic 时刻；`causal_event_id` 指向对应 `user_audio_chunk`。文件尾部静音和后续 VAD 静音继续发送，不延后这个边界。这个定义仍是客户端观测，不能称为服务端接收时刻。

`assistant_audio_start` 和首个有效 PCM chunk 共享同一个接收时间戳。播放器从内存拿到 PCM 后生成独立的 `assistant_playback_start/chunk/stop`，每个消费区间保留 PCM 引用、样本数、计划结束和调度迟到。`output.wav` 从 session_start 开始重建虚拟播放时间线，保留间隙；`output_received.wav` 只拼接收到的 PCM。二者都不声称是声卡实测。

manual 模式另有 `user_turn_commit`，区分 requested/submitted，计算客户端提交请求到首音频的时间。server VAD 模式不主动发送 commit/create。两种模式分组统计。

## 有效性与统计

[latency.yaml](../configs/latency.yaml) 在实测前固定以下初始规则，它们是本项目的开发 profile，不是通用行业标准。

| 项目 | 当前值 |
|---|---|
| 输入帧时长 | 20 ms |
| 最大开始发送迟到 | 20 ms |
| 最大一次发送耗时 | 20 ms |
| 最大虚拟播放调度迟到 | 20 ms |
| 追加 VAD 静音 | 1600 ms |
| 首音频超时 | 场景配置，当前 15 秒，从 speech end 计算 |
| 整个 case 上限 | 当前场景为 60 秒 |
| 播放收尾上限 | 当前场景为 15 秒 |
| warmup | 默认额外运行首个场景一次，保留工件但不计分 |

发送超过门槛会中止该 case 并记录 invalid，避免用突发补发掩盖输入调度失真。完整响应等待受整个 case 上限控制，不要求语音必须在首音频 timeout 内全部播放结束。返回了正常 response_end 却没有音频时记 `no_audio`，不伪装成超时。

指标包括 receive/playback TTFA 的 mean、P50/P90/P95/P99，manual commit-to-audio，以及可观察到的 VAD/response-start 分解。缺少对应事件的分解为 unavailable，不推算模型内部阶段耗时。

负 TTFA 保留为 premature，排除出正常非负分布，不截成 0。无首音频的超时保留分母和 censored 标记，分位数为 null 而不是 0。invalid、unknown、unsupported、warmup 均明确计数。Case Success Rate 的分母包含已尝试的 invalid/infra_failed；另报有效 case 的成功率和 coverage。它只检查此 latency 协议是否完成，不评价回答知识正确率。

evaluator `latency-0.2` 将测量后的关闭握手状态单独记为 `cleanup_warnings`。已封口日志、已观测的首音频/完整超时窗口、无测量期 fatal error，不会仅因客户端关闭未收到确认就被删除；测量期间断线、记录缺失或计时超限仍无效。超时必须有达到 speech-end + timeout 的单调钟证据，不能只信一个状态字符串。分布样本数与超时/有效数同时输出。

播放迟到门槛针对每个消费 quantum 的计时唤醒；接收音频到首次播放的队列/处理延迟单独记录和计入 TTFA_playback，不将它误称为声卡延迟。

以 model config、turn mode、控制策略、profile、播放模式、输入来源和边界方法/状态分组。自动边界得到的是对应估计边界下的 TTFA；不会与人工边界混合，也不合成为 Overall Score。每组仅 10 个样本时 nearest-rank P95/P99 是最大值，不能据此宣称稳定排名。

## 运行与离线重算

```bash
uv run --locked --extra qwen python -m benchmark.run \
  --model qwen-realtime --suite realtime \
  --scenario scenarios/realtime/basic.yaml

uv run --locked python -m benchmark.evaluate --run runs/<run_directory>
```

运行需要 adapter 对应的环境凭据；离线评价不需要 key，也不会调用模型。`--limit 1` 用于开发检查；`--repetitions N` 保存所有重复，不择优；`--warmups 0` 可关闭 warmup；`--turn-mode manual` 明确建立另一实验组。输入资产默认相对项目根目录，可用 `--asset-root` 指定。

```text
runs/<run_directory>/
  config.json
  manifest.json                # run_id、代码内容 hash、attempt 索引与 case manifest hash
  metrics.json                 # 最近一次评价的便捷导出
  cases/<scenario_id>/<attempt_id>/
    config.json
    scenario.json              # 冻结的已解析场景
    session_config.json
    manifest.json
    raw_events.jsonl
    events.jsonl
    input.wav
    output_received.wav
    output.wav
    transcript.json
    trial.json
    audio/*.pcm
    blobs/*.bin                # 原始输入 WAV 的精确副本，按内容 hash 命名
  evaluations/<evaluation_id>/
    config.json
    metrics.json
```

case 的 `manifest.complete` 表示录制文件完整，模型失败仍可有完整日志。run 的 `manifest.complete` 表示计划中的 attempts 都已结束，不等于所有 case 成功。进程提前终止留下 running manifest；正式汇总会拒绝它，已有 case 工件仍可单独检查。

离线评价先核对 run→case manifest hash，再检查日志、音频、source snapshot、时钟域和引用。evaluation_id 由输入 manifest、evaluator 源码/版本和评价配置确定；相同输入重算必须得到完全相同的 JSON。改变阈值会产生独立评价目录；已经中止而缺失后续数据的 case，不会因为放宽阈值自动变完整。

当前没有有效 Git 元数据，使用 Python 实现文件的内容 hash 标识本轮代码，不捏造 commit。源码事件保持不变，重评只写 evaluations 和根目录 metrics 导出。

## 验证范围

离线测试覆盖后台写入被阻塞/取消/失败、引用一致性、尾部静音边界、提前响应、超时、调度超限、warmup 排除、分组、工件损坏、虚拟播放间隙以及无需凭据的重算一致性。真实实测采用 10 条冻结中文 TTS，边界为自动估计，未进行人工标注可靠性验证。

Latency 执行路径只接受单轮场景。通用入口另支持 Phase 4 双轮 interruption、Phase 5 backchannel 和 Phase 6 duplex category；Agent 工具任务和更丰富报告继续扩展。

## 本轮真实结果

实测目录为 `runs/qwen-phase3-latency-001/`，Qwen3.5-Omni-Flash-Realtime / Tina、server VAD；10 条冻结中文 TTS，另有 1 条独立 warmup。profile 在运行前固定为 `configs/latency.yaml`，未因观察到失败而放宽门槛。

| 项目 | 结果 |
|---|---|
| 已尝试 / 有效 | 10 / 9 |
| pass / fail / invalid | 8 / 1 / 1 |
| Case Success Rate / coverage | 80% / 90% |
| 首音频超时 | 1，保留在有效样本分母，未填入零延迟 |
| 接收 TTFA 有观测值的样本 | 8 |
| receive TTFA mean / P50 / P95 | 4.823 / 5.148 / 8.848 秒 |
| virtual playback TTFA mean / P50 / P95 | 4.831 / 5.149 / 8.849 秒 |
| 后置关闭握手未确认 | 2，单独记录，未删除先前测量 |

`zh_latency_005` 在 15 秒内未收到首音频；`zh_latency_006` 出现约 126 ms 的虚拟播放调度迟到，按预设 20 ms 门槛标 invalid。其余输入发送的最大迟到约 2.2 ms。这里的 timing 只反映本次客户端观测，不能将服务端 VAD、模型生成与网络各自的耗时凭空拆开。

所有分位数都属于 `energy_rms_v1 / automatic` 边界组，使用 nearest-rank。n=8 时 P95/P99 是观测最大值；未人工验证边界和 TTS 发音，也未覆盖真人说话人与噪声。

实测后修正了一个 evaluator 问题：旧版把关闭握手失败直接当整条测量无效，导致一个真实超时从 timeout 计数中消失。`latency-0.2` 增加专门反例并从**同一份**工件重评，原评价目录仍保留。连续两次无 key 重算的 JSON 完全一致，22 个 raw/normalized 日志的哈希未变，没有为修正统计重新调用模型。

Phase 3 验收时有 68 项离线测试通过，另有 Ruff、资产哈希/格式校验、真实 WAV/manifest 校验。Phase 4 修改共享代码后的全量回归不能由这个历史数字代替；2026-09-19 的回归范围见 [Phase 4 状态](interruption-implementation.md)。共享计算环境中的偶发播放调度延迟仍需监测。
