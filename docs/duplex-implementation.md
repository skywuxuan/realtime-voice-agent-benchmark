# Turn Taking / Pause / Overlap 实现

更新日期：2026-09-20。当前 `duplex-0.2` 使用显式音频区间与真实发送边界，接入统一 run/evaluate/HTML report。已冻结 9 个可重复生成的中文 TTS 衍生场景，并完成 5 个停顿变体的真实 Qwen 实验。

## 运行

```bash
# 完全离线；复用已有 TTS，不重新合成，已有文件不覆盖
.venv/bin/python -m scripts.prepare_duplex

.venv/bin/python -m benchmark.run --model qwen-realtime --suite realtime \
  --scenario scenarios/realtime/pause_basic.yaml --output runs/my-pause --warmups 0

# 也可选择 turn_taking_basic.yaml 或 overlap_basic.yaml，分别运行
.venv/bin/python -m benchmark.evaluate --run runs/my-pause
```

`DASHSCOPE_API_KEY` 只在真实运行时需要。每次 run 使用新目录；输入 WAV、场景、配置、PCM、raw/normalized events、转写、metrics 与 report 按原有布局保存。重复离线评价输出一致，不改写源工件。

## 场景和发送边界

单个 `AudioAsset.regions` 明确记录区间 ID、kind 和 `bounds_samples`。Pause 使用 `kind=pause`；干扰说话使用 `kind=interferer` 并提供 ambient_speech、side_conversation 或 simultaneous_speech subtype。

Pause 必须严格位于整句 speech bounds 内。loader 校验标注不越过 WAV，sender 在所有区间边界拆帧，产生 `input_region_start/end`。它们使用相应音频块实际提交完成的 monotonic 时间和 causal event ID。整句话只有一个 `user_audio_start/end`，停顿结束不会被当作新用户回合。

在暂停窗口内发送的 PCM 标为 silence。区间外的音频不会因为衍生文件中出现停顿就被压缩或跳过。假定服务端已识别停顿、或用两个 user_audio_end 的间隔倒推出 pause 的旧算法已移除。

冻结场景包括：

- 原始“请用一句话介绍北京”的单轮 Turn Taking。
- 在相同音频同一位置插入 200/500/800/1200/2000ms 静音。它们是可重复的人工停顿扰动，不是自然停顿录音。
- 将另一条已冻结的中文问题按固定增益/偏移混入，提供 3 类干扰标签。两条源音频均为 Cherry 音色，不能宣称覆盖不同真人说话人。每个资产保留来源哈希、增益、偏移、混音算法版本；这几组增益不是校准 SNR。

## 指标与有效性

`response_gap_ms` 是与当前用户 turn 关联的 response start 减最终 user_audio_end。`ttfa_receive_ms`、`ttfa_playback_ms` 单独使用首音频接收和播放边界，不把 response.created 当音频输出。提前响应保留负值并判 fail，不先过滤负值再声称没有提前回答。

Pause 同时报告整句完成前的提前响应，以及每个标注停顿窗口内是否出现 response start。两者不同：模型可能在停顿刚结束、后半句还没说完时响应。这种样本整句 premature=true，但该 pause window 的 premature=false。指标只判断时序，不判断响应是否属于合理的语义 backchannel。

Overlap 报告用户发送音频与 assistant 虚拟播放、以及干扰区域与播放的交集。用户 chunk 的时间戳是提交完成，所以 sample interval 从该时刻向前投影一个 chunk 时长；不能把它当播放起点再向后加时长。计算先合并重叠区间，避免重复计数。该数值是客户端 handoff 与虚拟播放的近似，不声称服务端接收或声卡实测。

输入和播放调度超限、缺失/损坏工件、缺失 region 或错误因果引用为 invalid；有效模型超时保留分母和 censored，不填零 TTFA。自动/人工边界、模型配置、pause 长度、干扰类型分别分组。Overlap 的内容理解尚无独立 ASR/Judge，因此传输正常也只报 unknown，不能仅因收到音频就判鲁棒性 pass。

## 实测

工件 `runs/qwen-pause-20260920-001/`，模型 `qwen3.5-omni-flash-realtime` / Tina，原 20ms 发送/播放调度门槛未放宽。

| 停顿 | 结果 | response gap | 首音频相对整句结束 |
|---|---|---|---|
| 200ms | pass | 3790.211ms | 4038.471ms |
| 500ms | invalid | 无有效值 | 输入发送 deadline 超限 |
| 800ms | fail | -560.679ms | -315.135ms |
| 1200ms | fail | -955.554ms | -644.849ms |
| 2000ms | fail | -1609.832ms | -1317.075ms |

汇总为 5 attempted、4 eligible、1 pass、3 fail、1 invalid。失败和 invalid 全部保留，没有重试筛选通过样本。输入仅一个音色、一个文本和人工插入停顿，不能据此对模型中文能力作排名。

13 项专用测试覆盖负延迟、pause 内抢答、音频区间交集和去重、完整 run/evaluate、timeout、断线、事件损坏、标注越界、冻结资产逐字节重现。后续仍需自然停顿、多说话人干扰，以及内容理解的独立评价。

同日另有 `runs/qwen-turn-taking-20260920-001/` 的干净对照，1/1 pass。`runs/qwen-overlap-20260920-001/` 的3个混音样本全部 eligible：side_conversation 首音频超时记 fail，另外两个传输完成但内容评价 unknown；没有把传输正常当语义鲁棒性通过。这些 run 的完整性校验、密钥扫描和两次离线重评一致性均通过。
