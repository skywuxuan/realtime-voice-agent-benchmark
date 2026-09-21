# 文本测试集与 TTS 输入编译

本文定义如何把用户提供的中文文本测试集编译为 Realtime Voice Agent 的固定音频刺激。文本是数据集的源格式；模型运行时只接收 PCM 音频，不接收原文、参考答案或 oracle。

## 1. 数据流与计时边界

```text
text_corpus YAML
  -> TextDataset / TextScenario 校验
  -> TTS renderer
  -> content-addressed WAV + metadata
  -> runtime Scenario YAML
  -> RealtimeModelAdapter
  -> event log
  -> offline evaluator
```

编译器会在连接被测模型前完成整套数据的 TTS。TTS 请求、下载和 FFmpeg 解码耗时只写入渲染 metadata，不进入 TTFA、Stop Latency 或发送调度指标。运行产物记录 compilation manifest 的路径和 hash，每个 case 还记录实际 WAV hash、renderer profile 和派生 metadata。

这一层参考了 `qwen-audio-agent/examples/smart-cockpit/bench` 的做法：文本轮次先合成 PCM，再按固定 chunk 流式发送。这里额外加入严格 schema、事件触发的全双工动作、不可变缓存、输入边界和离线重评。没有复制其 runner 或厂商会话代码。

## 2. 半双工文本集

半双工 case 固定为一次用户输入和一次模型回答。批量数据建议使用紧凑的 `text_corpus`：

```yaml
kind: text_corpus
schema_version: '0.1'
dataset_id: half_duplex_text_corpus_v1
mode: half_duplex
category: latency
world:
  now: '2026-09-21T10:00:00+08:00'
  timezone: Asia/Shanghai
session:
  system_prompt: 请用一句简短、自然的中文回答用户。
  turn_mode: server_vad
  control_profile: native_server
tts_profile: qwen3_tts_flash_cherry_16k_v1
cases:
  - id: text_half_name_001
    text: 你好，请问你叫什么名字？
  - id: text_half_math_001
    text: 一加一等于几？
```

`scenario_id` 来自 case 的 `id`，必须全局稳定且只能含字母、数字、点、下划线和连字符。修改问题含义时应使用新的 ID 或数据集版本，不能覆盖已有运行。

带停顿的半双工输入使用显式 segment，不能在字符串里用省略号猜停顿时长：

```yaml
mode: half_duplex
category: pause
cases:
  - id: text_pause_0800_001
    segments:
      - type: text
        text: 帮我查一下
      - type: silence
        duration_ms: 800
      - type: text
        text: 明天下午上海到北京的高铁
```

每段文本分别合成并按自动语音边界裁切，再插入精确样本数的静音。metadata 保留子音频 render ID、裁切范围和 pause 样本区间。拼接处不具备真人连续发音的协同发音特征，因此最终 pause benchmark 仍需真人语音补充。

## 3. 全双工文本集

当前全双工文本集支持 `interruption` 和 `backchannel`，每个 case 包含初始问题和第二段刺激：

```yaml
kind: text_corpus
schema_version: '0.1'
dataset_id: full_duplex_text_corpus_v1
mode: full_duplex
category: interruption
world:
  now: '2026-09-21T10:00:00+08:00'
  timezone: Asia/Shanghai
session:
  system_prompt: 请用自然、简洁的中文交流。
  turn_mode: server_vad
  control_profile: native_server
tts_profile: qwen3_tts_flash_cherry_16k_v1
trigger_delay_ms: 350
trigger_timeout_ms: 20000
minimum_continuation_ms: 800
cases:
  - id: text_full_city_001
    initial_text: 请详细介绍北京适合周末游玩的地方，至少说三个。
    stimulus_text: 等一下，不是北京，我想问上海。
    expected_new_intent:
      city: 上海
    forbidden_old_intent:
      city: 北京
```

两段音频都在会话前合成。第一段在 `session_ready` 后播放；第二段绑定第一轮的 `assistant_playback_start`，等待 `trigger_delay_ms` 后发送。发送前必须确认目标回答仍在播放、仍在生成且本地已有至少 `minimum_continuation_ms` 的后续音频证据，否则该 attempt 标为 invalid，不能算模型中断失败。

Backchannel 只需把 category 改为 `backchannel`，并将 `stimulus_text` 写成“嗯嗯”“你继续”等。其 oracle 自动要求旧回答继续；runner 不因知道标签而调用客户端 cancel。

需要逐 case 控制 trigger、precondition、oracle 或 termination 时，使用 `kind: text_scenario` 详细格式。示例位于 `datasets/source/half_duplex/basic.yaml`、`pause.yaml` 和 `datasets/source/full_duplex/interruption.yaml`。

## 4. 渲染、冻结与运行

Qwen TTS profile 位于 `configs/tts/qwen-cherry.yaml`。API key 只从官方环境变量 `DASHSCOPE_API_KEY` 读取。

先生成并冻结整套音频：

```bash
.venv/bin/python -m dataset.render \
  --source datasets/source/half_duplex/corpus.yaml \
  --tts-profile configs/tts/qwen-cherry.yaml
```

确认缓存完整且绝不调用 TTS 服务：

```bash
.venv/bin/python -m dataset.render \
  --source datasets/source/half_duplex/corpus.yaml \
  --tts-profile configs/tts/qwen-cherry.yaml \
  --cache-only
```

直接从文本源运行真实 benchmark：

```bash
.venv/bin/python -m benchmark.run \
  --model qwen-realtime --suite realtime \
  --scenario datasets/source/half_duplex/corpus.yaml \
  --tts-profile configs/tts/qwen-cherry.yaml \
  --render-missing --warmups 0
```

第一次使用 `--render-missing` 允许补齐缓存。正式对比模型时先完成并检查渲染，然后去掉该参数；缺少任一 WAV 会在连接被测模型前失败。相同数据集、TTS profile 和缓存应供所有 target adapter 共用。

## 5. 缓存与可重复性

`datasets/rendered/<profile_id>/` 保存 WAV 与同名 JSON。TTS 音频身份由文本、完整 profile、renderer/FFmpeg 指纹和边界 recipe 决定；供应商输出 URL 与 key 不保存。第一次成功渲染后文件不可覆盖，供应商再次合成可能不同也不会改变旧资产。

`scenarios/compiled/<dataset_id>/<compilation_id>/` 保存展开后的 `source.json`、runtime Scenario、suite 和 manifest。compilation ID 还包含编译器及 schema 源码指纹，因此执行逻辑变化会生成新目录，同时继续复用内容相同的冻结音频。

输出中的 `cache_hits`、`cache_misses` 和 `provider_calls` 分开记录。segment 拼接可能产生本地 cache miss，但 `provider_calls=0` 表示没有发生收费的 TTS 请求。

## 6. 当前限制

- 只实现 Qwen TTS renderer；这不影响 target adapter 抽象，后续 renderer 通过独立 registry 增加。
- 紧凑全双工格式当前只有两段输入和基于助手播放开始的触发。多次插话、绝对时间线和 side conversation 应扩展 action DSL，不能塞进文本字段。
- `energy_rms_v1` 是自动能量边界，不是人工语义边界。正式排行榜需要抽听或人工修订并单独分组。
- 单一 TTS 音色只能验证基础设施和控制行为，不能替代真人 speaker、口音、噪声和自然犹豫数据。
- 文本 source 不自动提供开放回答的正确性标准。知识、推理和自然度仍需 rule、reference、judge 与人工抽样层。

## 7. 当前真实验证

2026-09-21 的 `runs/qwen-text-corpus-half-20260921-001/` 从紧凑 corpus 直接运行两条 Qwen Realtime case，2 条均 completed 且 evaluator pass。输入编译为 2 次 cache hit、0 次 provider call；receive TTFA 两条样本为 1200.236ms 和 1228.364ms。离线重评得到相同 evaluation ID。

`runs/qwen-text-corpus-full-20260921-001/` 从紧凑全双工 corpus 直接运行一条 interruption case。刺激 eligible，中断检测 confirmed，Stop Latency 381.465ms，Residual Audio 362.878ms；新回答未在期限内完成，因此 case 为 fail、context switch 为 unknown。离线重评 ID 不变，运行阶段同样为 2 次 cache hit、0 次 provider call。

详细格式还完成了单条半双工 latency、全双工 interruption 和 800ms pause 的真实运行。此前 interruption 的行为语义规则保守判为 unknown；pause case 因目标 response 未完成判 fail。这些失败和 unknown 保留为模型/场景证据，不改写为编译成功。样本量和音色覆盖不足以用于模型排名。
