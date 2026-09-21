# Qwen-first Integration Plan

状态：**Phase 2/3 有历史实测；Phase 4 已完成主要失败路径回归及完整 Qwen 双轮中断运行，语义规则与多用例覆盖继续完善**。更新日期：2026-09-19。

统一契约已接入 [Qwen Adapter](../adapters/qwen/adapter.py)，transport 使用锁定版本的异步 WebSocket 库。协议依据为官方 SDK/示例与实际返回。第 8 节记录 Phase 2 历史结果；当前 latency/interruption 的运行与限制分别见 [Phase 3](latency-implementation.md)、[Phase 4](interruption-implementation.md)。

本文件区分商业 **Qwen Omni Realtime API** 与开源 **Qwen3-Omni**。统一 adapter 不意味着二者共享 WebSocket 事件、鉴权或打断能力。MVP 只实现商业 Realtime；本地 Omni 在 Phase 11。

## 1. 官方依据与核验范围

| 来源 | 固定版本 | 实际读取内容 |
|---|---|---|
| [DashScope Python SDK][sdk] | `b0b4469e13dd1b0c1d842f99fbfc6a300c5dc760` | Phase 2 重新核验的 HEAD，SDK 版本 1.27.6；Omni realtime 文件与初次调研 commit 一致 |
| [阿里云官方语音示例][demo] | `1082942345c555429ee61c04b8c585f12e06dab2` | `samples/conversation/omni/python/` 下 server VAD、manual、function call、播放器示例 |
| [Qwen3-Omni][omni] | `e4235853125589c789f06a2dd83e9f4126df5e9d` | README、web_demo、tool cookbook、仓库内技术报告 |
| [vLLM-Omni][vllm] | `e3be42e052e88d052d21cf7a24ee84c70f020fd6` | Qwen 部署配置、realtime/full-duplex 文档、实时客户端 |

阿里云示例指向的 [官方 Realtime 文档入口](https://help.aliyun.com/zh/model-studio/realtime) 在初次研究中因 TLS 失败未读到；Phase 2 重试网页工具仍遇到搜索后端错误。已通过 GitHub 核对官方 SDK/示例当前 commit，并成功连接 DashScope 服务。下文区分源码与实测证据，未据此推定其他模型/区域能力。

## 2. 商业 API 已验证的协议事实

### 2.1 连接、模型、鉴权

**[源码验证]** `OmniRealtimeConversation` 默认构造：

```text
wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=<model>
Authorization: Bearer <api_key>
```

SDK `url` 参数接受 base URL，内部追加 `?model=...`；adapter 不把已有 query 的完整 URL 再传进去。自定义地域 endpoint 必须单独从官方资料核验，不能按字符串猜测。SDK README 的通用 region helper 不证明这个 audio class 自动使用相同 endpoint，需以其构造函数为准。[SDK 源码][sdk-protocol]

**[源码验证]** 环境变量名为 `DASHSCOPE_API_KEY`，官方 README 与实时 demo 均使用它。本项目只允许从环境读取，缺少时给明确配置错误，不采用官方 demo 中手填 key 的 fallback；根目录 [.env.example](../.env.example) 只提供空占位。凭据不写入 config/raw events/报告。

**[源码验证]** 当前官方 `run_server_vad.py` 使用 `qwen3.5-omni-flash-realtime`、`Chelsie`；`run_with_function_call.py` 使用 `qwen3.5-omni-plus-realtime`、`Tina`。它们是本次看到的示例配置，不是账户可用性承诺。MVP 候选先采用 server-VAD 示例中的 Flash 配置，启动前真实 probe；`qwen-realtime` 仅作为本项目 CLI adapter alias，配置中必须保存实际模型 ID。[VAD 示例][demo-vad]、[工具示例][demo-tools]

**[实测验证]** 北京 endpoint 下本账户可以使用 `qwen3.5-omni-flash-realtime`。`Chelsie` 虽然被 session.updated 原样回显，生成时仍返回 `Voice 'Chelsie' is not supported.`；改用 session.created 返回的默认音色 `Tina` 后，音频往返成功。当前 profile 因此默认 Tina，并将配置回显与实际能力验证分开。

**[待实验]** 其他模型/区域、可固定日期的 snapshot、其他 voice/temperature/sampling、长连接限制与限流。未公开 API 版本用 null + reason，不把 URL 中的 `v1` 当模型 revision。本次按用户授权读取指定文件中的 key，仅注入启动进程环境，进行了少量真实请求；代码、日志和报告不保存 key。

### 2.2 音频与会话配置

**[源码验证]** SDK legacy 路径与官方 demo 使用：

| 方向 | 已核验的示例配置 |
|---|---|
| 输入 | mono、16-bit PCM、16 kHz，base64 放入 JSON `audio` |
| 输出 | mono、16-bit PCM、24 kHz，base64 `response.audio.delta.delta` |
| legacy session fields | `input_audio_format: pcm16`、`output_audio_format: pcm16` |
| output modalities | audio、text |
| VAD | `turn_detection.type: server_vad` |

SDK 的 legacy `pcm16` 字段本身不携带采样率；16/24 kHz 的依据来自 AudioFormat 默认值与实际官方例子。MVP 显式记录这些假定并通过播放时长/波形校验。内部规范为 PCM signed 16-bit little-endian；native PCM 字节序、服务端接受范围仍列入 Phase 2 协议 probe，不能把 WAV 文件头直接塞入 PCM append。

SDK 默认配置参数包括 threshold=0.2、prefix_padding_ms=300、silence_duration_ms=800；这些是客户端默认值，不代表最佳 benchmark 配置。首次采用可复现的显式 profile，冻结前做 pause/backchannel 预试验，正式样本不可逐 case 调参。

**[源码验证，但不可推定服务端支持]** 当前 SDK 新增 `AudioFormatConfig`，可发送 `session.audio.input/output.format`，接受任意格式/采样率值且不严格验证。新旧格式路径不能混用。MVP 先选官方 Omni 示例已使用的 legacy 路径；并不因为 SDK 接受 8/16/24/48 kHz、WAV 或其他字符串，就登记目标模型全部支持。[音频配置实现][sdk-protocol]

**[实测验证]** 当前服务接受 20 ms 分帧 PCM，返回 `response.audio.delta` base64 PCM；`qwen3-asr-flash-realtime` 输入转写返回“你叫什么名字？”。输出按官方 24 kHz mono PCM16 profile 保存为有效 WAV。采样率未在 session.updated 中显式回显，仍列入 unverified，而不是声称服务端确认了数值。frame 极限、前导静音、长会话及严格 pacing 仍需 Phase 3 验证；不能直接继承官方 demo 的 200 ms 输入/100 ms 播放缓冲。

### 2.3 控制命令

| 统一操作 | 官方 SDK 源码中实际序列化的协议 | 已知边界 |
|---|---|---|
| configure | `session.update` + `session` | 已观测 session.updated；关闭 VAD 时 null 字段会被省略，保留为未显式回显 |
| send_audio | `input_audio_buffer.append` + base64 `audio` | 输入是编码后的 PCM 数据，不是 WAV 路径 |
| commit_turn，manual | `input_audio_buffer.commit`，随后 `response.create` | server VAD 模式 SDK 注释说明由服务端提交/生成 |
| clear pending input | `input_audio_buffer.clear` | 不等价于取消输出，不作为通用 interrupt 的默认行为 |
| interrupt（主动） | `response.cancel` | 已观测 response.done.status=cancelled，status_details.reason=client_cancelled；请求本身不等于确认 |
| send_tool_result | `conversation.item.create` 中的 `function_call_output`，然后 `response.create` | 官方工具示例验证；需 call_id 关联 |
| SDK finish 方法 | `session.finish`，SDK 处理 `session.finished` | 当前模型实测返回 invalid_value，Adapter 不发送此命令 |
| close | 标准 WebSocket close | 当前 Adapter 的实际收尾方式，已观察 code=1000；响应状态与关闭状态分别保存 |

以上是从 SDK 方法读取的实际事件名，不由 OpenAI API 的相似名称推断。[SDK][sdk-protocol]

### 2.4 事件映射计划

| 官方源码/示例里已看到的 vendor event | Normalized event | 映射规则 |
|---|---|---|
| `session.created` | session_start | 保存远端 session id 与原始配置 |
| `session.updated` | session_configured | 实测确认后保存 requested/effective/unverified |
| `input_audio_buffer.speech_started` | vad_start | 只证明有语音；不得直接发 interrupt_detected |
| `input_audio_buffer.speech_stopped` | vad_end | 时间为本地收到事件；原始音频 offset 另存 |
| `conversation.item.input_audio_transcription.completed` | user_text_done | 与参考文本分开，保留 item 关联 |
| `response.created` | assistant_response_start | SDK 读取 response.id |
| `response.audio.delta` | assistant_audio_chunk；首块同时 audio_start | base64 解码有效后，保留消息到达时间；错误另报 |
| `response.audio.done` | assistant_audio_end | 已实测，不当作实际播放停止 |
| `response.audio_transcript.delta` | assistant_text_delta，spoken_transcript | 与普通文本输出分 channel |
| `response.audio_transcript.done` | assistant_text_done，spoken_transcript | 已实测，供应商转写不证明用户已听到全文 |
| `response.text.delta` | assistant_text_delta，text_response | 官方 function-call 示例处理该类型 |
| `response.function_call_arguments.done` | tool_call_start/arguments/end | 无早期 delta 时同时派生并标记 single_message |
| `response.done` | assistant_response_end | status/输出内容按真实 payload 解析；不能无条件映射 completed |
| `session.finished` | 正常 finish 证据 | session_end 由统一生命周期收尾 |
| `error` | error | 错误消息脱敏，保留 code 与 scope |

**[待核验]** 工具 arguments delta、item truncate、原生语义打断与多轮恢复。Phase 2 已据真实 trace 补全 session.updated、audio/transcript done 和取消 status。2026-09-20 已映射完整工具调用并实现结果回传；协议单测、sealed-artifact 测试以及 Plus 模型真实天气工具闭环通过，增量参数事件仍仅保留 raw。

若没有独立 audio_done，在 response.done 时可生成 `assistant_audio_end`，明确 `completion_source=response_done`，其时间只是接收终止证据，不能当最后一个样本到达或播放的时刻。若没有最终 transcript 事件，可从完整 delta 派生 text_done 并标来源；取消时标 partial。

SDK 内部 `get_last_first_audio_delay()` 使用 `time.time()`，起点为 response.created，不符合本项目“user_audio_end 到首音频”的定义。只作为 raw/diagnostic 保存，不作为主要指标。

## 3. MVP Adapter 实施方案

**[已实现]** 采用提案中允许的 asyncio WebSocket 备用路线。官方 SDK 的发送序列化入口是私有方法，没有公开的双向原始消息 hook；直接在 adapter 内使用 `websockets==15.0.1` 可以完整记录实际收发 JSON 和观测时间，无需全局 monkeypatch 或 fork SDK。协议仍来自固定官方源码与实测，Runner 不依赖厂商 SDK。

职责分层：

1. 配置层解析实际 model、base URL、voice、VAD、输入输出格式，仅从环境获取 key。
2. 独立接收任务在 `recv()` 返回完整消息时立即取双时钟时间，放入有界队列；磁盘写入和归一化在另一任务完成。
3. 保存实际发送的 JSON（包含 command event_id）及收到的原始字段；HTTP Authorization 头不写日志。归一化音频保存 PCM 引用，重复 vendor event_id 保留 raw 但不会重复产生音频。
4. raw 指完整 WebSocket 消息中的厂商字段，不是 TLS/TCP 抓包。归一化/落盘延迟不改写接收时间；队列溢出使运行失败，不静默丢包。Phase 3 runner 已通过 `BufferedArtifacts` 将落盘移出收发和播放路径；旧 connection probe 保留原诊断行为，不用于正式统计。
5. ID map 管理 session/input item/response/output item/call，处理重复、迟到、未知事件。错误格式不能静默跳过后继续给有效分数。
6. close 与 abort 有独立超时，异常结束保留原始事件及已收音频。

公共基类保证单接收者；关闭后可继续排空已产生事件直到 EOF。通用 `connection_probe.py` 不识别 Qwen 事件名，`qwen_probe.py` 只负责组装模型配置和 adapter。未引入中转平台、网页前端或声卡依赖。

### Native 与 client interruption 策略

2026-09-19：已验证先取消、后 VAD 的到达顺序。仅最新 response、无 client cancel、明确 turn reason、一秒关联窗口内的后到 VAD 可补 confirmed detection；更旧/未知原因保留未确认。播放器按 response 丢弃未消费样本，drain 截断明确标记 case_abort。完整回归及真实工件见 [Phase 4 实现说明](interruption-implementation.md)。

**[源码验证]** 官方 `run_server_vad.py` 收到 `speech_started` 就调用 `cancel_playing()`，播放器清理两个队列；不是对 backchannel 的语义判断。SDK 另有主动 `cancel_response()`，与本地播放取消是不同动作。[VAD 示例][demo-vad]、[播放器][demo-player]

**[已实现，限固定双轮场景]** native_server track 不读取 oracle 来 cancel。按真实服务取消证据丢弃旧 response 的未消费 PCM，并记录每段样本引用；若确认事件不足，则先报告观测能力不足，保留纯接收和播放证据。另跑 client_vad_flush profile 可以复现官方 demo 用户体验，但不能合并为模型语义打断率。

client_forced track 调用 `interrupt()` 仅测控制路径，不能拿来填自然 interruption detection 或 backchannel FIR。context truncation、播放进度上报能力尚未验证；缺失时必须承认服务端可能保留用户未听见的生成内容，不能假定已回滚。

## 4. 真实接入验证清单与退出条件

以下是 Phase 2–6 的验收路线；连接、配置、manual/VAD 音频往返、主动取消、固定双轮中断和一次合成 backchannel 已实测，异常断线/溢出等由离线测试覆盖。真实中文 backchannel 语料、pause/overlap 多用例仍待扩展。任何测试均不打印或保存密钥。

| 顺序 | 最小实验 | 需要保留的证据 |
|---|---|---|
| 1 | 连接官方 base URL 与账户可用模型 | session id、原始创建事件、模型/地域配置、错误 code |
| 2 | 发送显式配置，等待服务端实际确认/错误 | requested/effective/unverified；无法确认时不伪造成功 |
| 3 | manual 模式发送一条冻结中文 PCM，commit/create | 出站事件、输入 hash、收到音频块、独立解码/时长检查 |
| 4 | server VAD 同音频，持续送尾部静音 | speech start/stop、自动响应；无重复 commit |
| 5 | 收到第一块就交 virtual playback，持续收流 | 每块到达/消费时间、buffer 水位、完整 output，确认真实 streaming |
| 6 | 本地主动 cancel 一次 | 请求、确认/失败、迟到音频、上下文后续；单独 control track |
| 7 | 输出中仅注入中文打断音频 | 原生检测/停止/新意图是否成立，speech_started 与 cancelled 区分 |
| 8 | 输出中注入“嗯嗯/你继续” | 是否继续、服务端取消与客户端 flush 分因 |
| 9 | 丢失连接/timeout 收尾 | partial artifact、错误状态、重试新 attempt |
| Phase 7 前 | 验证实际模型 tool call/result 循环 | call_id、参数、工具结果注入、继续响应；无需提前实现完整工具平台 |

真实凭据与网络条件已验证，Phase 2 连接层完成。Phase 3 已补齐单轮场景执行、虚拟播放和分组 TTFA；Phase 4 中断、Phase 5 合成 backchannel、Phase 6 duplex 离线指标已接入统一入口。真实中文 backchannel、多样本统计和更丰富 HTML report 仍待扩展。

## 5. Commercial Tool Use 的后续计划

**[源码验证]** 官方 `run_with_function_call.py` 在 `update_session(tools=TOOLS)` 传工具 schema；收 `response.function_call_arguments.done`，取得函数名、arguments 和 call_id；`create_item({type: function_call_output, call_id, output})` 注入结果，再 `create_response()`。这个例子证明官方至少提供该路径，不能推定所有 Realtime 模型版本都支持。[函数调用示例][demo-tools]

本项目不会复制示例的同步工具执行主循环或固定回答工具。音频接收/发送继续运行，独立接收任务持续记录，deterministic tool runtime 立即执行；每次调用/结果落同一审计，模型不得访问 expected final state。Phase 7 已用 deterministic weather/train/calendar runtime 验证 single call、最终状态 hash 和失败 fixture；2026-09-20 已接入 Qwen function-call adapter 的执行路径，按 response 分组回传全部结果后继续生成；能力初始依据为 docs，已有 Plus 模型真实天气、查车→日历、执行中改口工件，见 [Agent 实现](agent-implementation.md)。

工具参数片段只有完整解析并校验后才能执行；不把部分 JSON 的默认值补成正确参数。工具执行取消、响应取消和用户修正是三个状态机，不共享一个布尔 `cancelled`。

## 6. 开源 Qwen3-Omni 后续路线

### 模型与本地接口 [源码验证]

`Qwen3-Omni-30B-A3B-Instruct` 有 Thinker+Talker，能处理音频并输出语音；Thinking、Captioner 变体是 thinker-only，不进入需要语音输出的同一组。官方 processor 消息格式可含 `{"type":"audio","audio":"path-or-url"}`，使用 `Qwen3OmniMoeProcessor`、`Qwen3OmniMoeForConditionalGeneration` 与 `qwen_omni_utils.process_mm_info`。[README][omni-readme]

Transformers 示例 `model.generate(...)` 返回 `(text_ids, audio)`，示例将音频写为 24 kHz WAV。当前 README 推荐 Transformers >=5.2.0、FlashAttention2、FFmpeg；这些是后续独立推理环境依赖，不加进商业 API MVP。[推理示例][omni-readme]

`web_demo.py` 的 Transformers 路径等整次 generate 完成再写 WAV。不能把整段 WAV 切块播放后声称首块在生成期间流出；普通 vLLM thinker-only 路线也不能当语音输出服务。[web demo][omni-web]

### 流式与 realtime 能力边界 [源码验证]

vLLM-Omni 的 Qwen pipeline 分 Thinker/Talker/Code2Wav，配置 `async_chunk: true`；其在线示例支持流式文本/音频及 WebSocket realtime client。已读配置标明默认方案在 2×H100 验证，stage 分配到两块 GPU；这只是该配置的验证环境，不是本项目硬件承诺。[部署配置][vllm-deploy]

官方 realtime 文档将该接口称为 turn-based streaming。Qwen client 使用 mono PCM16 16 kHz 输入，final/non-final commit 管理输入流；输出事件使用 `response.output_audio.delta/done`，读取 `sample_rate_hz`，不能套用商业 API 的 `response.audio.delta`。[realtime 文档][vllm-realtime]、[客户端][vllm-client]

该 turn API 没有文档承诺 barge-in controls、duplex session resume、playback acknowledgement 或 overlap policy。官方 full-duplex 文档中 native duplex plugin 当前面向 MiniCPM-o 4.5，Qwen 仍为 turn mode；serving-side VAD 不证明 Qwen 原生语义 full duplex。[full-duplex 文档][vllm-duplex]

**[方案]** Phase 11 独立 `QwenOmniAdapter`，按所选 backend 声明能力。Transformers 整段生成可进入质量 track；vLLM-Omni streaming 进入其合法 realtime 子集；未支持的 native interruption 不强制伪装。未来服务端能力更新后再补 probe，不为了首版接它推迟商业 Qwen。

### GPU 与 latency [源码验证 / 待实验]

Qwen README 的显存表是 BF16 + FlashAttention2 下 **15/30/60/120 秒视频**输入的理论最低占用，Instruct 对应 78.85/88.52/107.74/144.81 GB。不能把它写成纯音频最小显存或保证单卡 80 GB 可部署。关闭 Talker 可省显存，但失去语音输出。[显存说明][omni-readme]

官方技术报告的 234 ms 是理论 first-packet latency，和本项目包含输入端点、网络、队列的 TTFA 不同，不作为预计分数。报告的阶段划分可供后续部署 profiling 参考。[仓库内报告][omni-report]

本地实验必须记录 model revision、后端 commit、CUDA/GPU 型号数量、dtype/量化、分阶段配置、显存、并发、warmup、chunk/pacing/VAD 策略。实际音频输入吞吐、首块延迟、长流内存、停止/恢复、中文工具修正均待测。此次没有下载权重或启动 GPU。

### 工具示例 [源码验证]

`cookbooks/audio_function_call.ipynb` 把工具 schema 放进 prompt，让模型输出 `<tool_call>` 包裹的 JSON。可在 adapter 解析层参考，但 notebook 没有本项目所需的状态执行/故障恢复闭环，不能将文本调用输出视为任务完成。[tool cookbook][omni-tool]

## 7. Step / Doubao 的边界

本次不实现它们，也不填未经官方文档确认的 endpoint、event、音频格式或环境变量名。Phase 9/10 各自先核验最新官方 SDK/示例、完成 capability probe，再通过同一 event/scenario/evaluator 契约。禁止为了表面接口一致，在 Runner 内加厂商分支；协议差异留在 adapter 内。

## 8. Phase 2 实现与真实验证记录

日期 2026-09-17，模型 `qwen3.5-omni-flash-realtime`，音色 `Tina`，北京 endpoint。输入为阿里云官方 demo 的 `q1_16khz.pcm`，转换为 WAV 后输入；约 2.076 秒，供应商识别文本为“你叫什么名字？”。录音的真人/TTS 来源未核验，仅作为连通性 fixture，不纳入正式数据集。原始来源 commit/URL 与音频 hash 写入每次 config。

| 实验 | 本地工件目录 | 实际结果 |
|---|---|---|
| manual 初次适配 | `runs/qwen-phase2-manual-001/` | failed；发现 null VAD 回显被省略及 session.finish 不被支持；保留 partial artifacts |
| manual | `runs/qwen-phase2-manual-002/` | completed；12 块音频、3.76 秒输出；manifest complete |
| server VAD | `runs/qwen-phase2-vad-001/` | completed；23 块音频、7.12 秒输出；观察到 vad_start/vad_end；出站仅 session.update 与 audio append，没有 client commit/create |
| client cancel | `runs/qwen-phase2-cancel-001/` | 首块后请求取消；收到 cancelled/client_cancelled 确认；保存 2 块共 640 ms 已收到的音频 |

三个成功目录都可用 `read_recording()` 离线校验。输出为有效的 24 kHz 单声道 PCM16 WAV，manual/VAD 音频均非静音。每次保存 config、session_config、input.wav、output_received.wav、双向 raw/normalized JSONL、transcript、probe.json、PCM 与 manifest。工件在 `.gitignore` 排除的 runs 目录，未提交真实录音或凭据。

这些数值是连接验证证据。取消后的已接收时长不是 Residual Audio Duration，没有播放器就不能报告 Stop Latency；供应商完整文本也不等于已播放的内容。当前 input end 是文件结束代理，没有人工 speech-end 标注，因此 file-end 延迟不命名为 TTFA，也不计算 P50/P95。

Phase 2 manual probe 最大单帧发送迟到约 158 ms，VAD probe 约 2.1 ms。Phase 3 已拆开发送与写入，固定 warmup 和调度门槛，完整实测中最大发送迟到约 2.2 ms；另外捕获到一次播放调度迟到并标 invalid。旧 probe 仍不直接当正式 latency 数据。

Phase 3 还观察到两次客户端收尾未收到 WebSocket close 确认，code=1006。已观测的完整回答或首音频超时不会仅因后置关闭状态被从统计中删除；evaluator 将其作为 cleanup warning，测量期间的 fatal error 仍令 case 无效。该规则和来源日志都可离线检查。

当前回合关联保守处理：manual 单个未决 turn 使用 `inferred_serial_turn`；server VAD 单输入回合能关联 input item，复杂多输入回合缺乏唯一证据时保留 ambiguous，不盲配最近响应。后续自然打断需扩展按服务端音频 offset 与输入样本区间关联。

工具结果注入已按官方固定示例实现并通过 fake WebSocket 验证；Plus 的 weather 工具调用已有单场景实测；其他模型/工具组合、上下文截断、playback acknowledgement、native full duplex 未实测确认。主动取消确认只证明控制通道可用，不计入自然打断率或 backchannel FIR。2026-09-19 完整双轮工件在 `runs/qwen-phase4-interruption-20260919-001/`，confirmed detection、stop 371.013ms、residual 355.655ms，新回答全部播放。因为回答澄清时提及北京，保守文本规则保留 context unknown。历史 `20260918-005` 的播放被截断，0.3 将旧 pass 修正为 unknown，原评价与日志保持不变。这些均为单样本框架验证。

## 9. Qwen Audio 3.0 Realtime Flash 实测

2026-09-21 依据用户指定的 `qwen-audio-agent` cockpit demo 接入 `qwen-audio-3.0-realtime-flash`。参考实现固定了 `longanqian`、16kHz PCM 输入、24kHz PCM 输出、smart-turn 和 Function Calling；本项目实际连接确认同一北京 Realtime endpoint 可用。

Audio 3.0 的会话与 Qwen3.5 Omni 有几处实际差异：session 必须包含 text modality；smart-turn 回显 2000ms 静音窗口；`session.updated` 不回显 input audio format；user item 建立后需要显式创建首轮 response；工具结果续答会遇到单 response slot busy 竞态。所有分支都封装在 Qwen adapter，runner 未加入厂商事件名。probe `001`–`017` 均保留，分别记录配置回显、尾静音、调度、modalities、response 时序、busy 以及重复调用策略实验；其中既有 infra/invalid，也有完成会话但严格任务失败的样本，不能混为模型得分。

最终 5-case 工件 `runs/qwen-audio3-cockpit-smoke-20260921-001/` 全部完成音频输入、工具结果回传和语音输出。首调用函数及参数 5/5 正确，但每条随后重复同一调用一次，严格 Task Completion 0/5。完整数据编译和指标见 [座舱 Benchmark](cockpit-benchmark.md)。


[sdk]: https://github.com/dashscope/dashscope-sdk-python/tree/b0b4469e13dd1b0c1d842f99fbfc6a300c5dc760
[sdk-protocol]: https://github.com/dashscope/dashscope-sdk-python/blob/b0b4469e13dd1b0c1d842f99fbfc6a300c5dc760/dashscope/audio/qwen_omni/omni_realtime.py
[demo]: https://github.com/aliyun/alibabacloud-bailian-speech-demo/tree/1082942345c555429ee61c04b8c585f12e06dab2/samples/conversation/omni/python
[demo-vad]: https://github.com/aliyun/alibabacloud-bailian-speech-demo/blob/1082942345c555429ee61c04b8c585f12e06dab2/samples/conversation/omni/python/run_server_vad.py
[demo-tools]: https://github.com/aliyun/alibabacloud-bailian-speech-demo/blob/1082942345c555429ee61c04b8c585f12e06dab2/samples/conversation/omni/python/run_with_function_call.py
[demo-player]: https://github.com/aliyun/alibabacloud-bailian-speech-demo/blob/1082942345c555429ee61c04b8c585f12e06dab2/samples/conversation/omni/python/B64PCMPlayer.py
[omni]: https://github.com/QwenLM/Qwen3-Omni/tree/e4235853125589c789f06a2dd83e9f4126df5e9d
[omni-readme]: https://github.com/QwenLM/Qwen3-Omni/blob/e4235853125589c789f06a2dd83e9f4126df5e9d/README.md
[omni-web]: https://github.com/QwenLM/Qwen3-Omni/blob/e4235853125589c789f06a2dd83e9f4126df5e9d/web_demo.py
[omni-report]: https://github.com/QwenLM/Qwen3-Omni/blob/e4235853125589c789f06a2dd83e9f4126df5e9d/assets/Qwen3_Omni.pdf
[omni-tool]: https://github.com/QwenLM/Qwen3-Omni/blob/e4235853125589c789f06a2dd83e9f4126df5e9d/cookbooks/audio_function_call.ipynb
[vllm]: https://github.com/vllm-project/vllm-omni/tree/e3be42e052e88d052d21cf7a24ee84c70f020fd6
[vllm-deploy]: https://github.com/vllm-project/vllm-omni/blob/e3be42e052e88d052d21cf7a24ee84c70f020fd6/vllm_omni/deploy/qwen3_omni_moe.yaml
[vllm-realtime]: https://github.com/vllm-project/vllm-omni/blob/e3be42e052e88d052d21cf7a24ee84c70f020fd6/docs/serving/realtime_api.md
[vllm-client]: https://github.com/vllm-project/vllm-omni/blob/e3be42e052e88d052d21cf7a24ee84c70f020fd6/examples/online_serving/qwen3_omni/openai_realtime_client.py
[vllm-duplex]: https://github.com/vllm-project/vllm-omni/blob/e3be42e052e88d052d21cf7a24ee84c70f020fd6/docs/serving/full_duplex_api.md


2026-09-20 高级 Agent 实验已接入统一 `benchmark.run --suite agent`。旧工具结果仍送回服务，但最新输入属于新回合时不再为旧工具 response 发送 response.create，避免重启已被用户改口的问题；这是显式 adapter continuation policy，不是模型语义能力。实际模型通过新语音产生天津调用，时序与参数证据见 [Agent 实现](agent-implementation.md)。长输入20ms分帧发生写入背压后，按官方示例单独验证200ms分帧并通过，20ms调度误差门槛不变；该配置不合并为20ms输入实验。
