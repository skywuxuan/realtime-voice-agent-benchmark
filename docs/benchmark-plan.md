# Benchmark 定义与分阶段实施计划

状态：**Phase 1–8 已有不同程度实现和实测；文本源、会话前 TTS、冻结缓存及半/全双工 Scenario 编译已实现；Phase 9–11 只有明确的 deferred adapter 边界**。更新日期：2026-09-21。

系统结构见 [architecture](architecture.md)，事件与场景字段见 [event-schema](event-schema.md)，商业 Qwen 的实际协议依据见 [qwen-integration](qwen-integration.md)。

## 1. 范围与实验单元

第一版只做 Qwen Realtime 的 normal latency、interruption、user backchannel，每类先 1 个开发用例，再扩到约 10 个冻结用例，总计约 30 个。30 cases 是基础设施验证集，不代表中文人群、口音或行业任务覆盖。

三类结果独立展示：Realtime Metrics、Agent Metrics、Response Quality Metrics；尚未运行的维度为 `not_run`，不填零，不合成 Overall Score。Realtime 的 case success 仅是该 suite 的验收条件，不宣称总体回答质量优秀。

实验单元是 `(scenario_id, scenario_version, model_config_hash, control_profile, attempt_id)`。同一 case 多次运行保存全部 attempts，禁止只取最优一次。MVP 默认并发 1，以免限流与本机调度竞争影响 latency；warmup 独立标记并排除出正式汇总。对比模型使用相同音频、场景、评价器及已声明的控制策略，VAD/格式差异必须公开。

### 统计与缺失值契约

每项至少输出 `metric_version`、unit、value、numerator/denominator 或 n、eligible_count、unknown_count、invalid_count、timeout_count、unsupported_count、evidence_event_ids 与排除原因。比率分母为零时 value=null。没有首音频、没有取消确认等情况不能用 0 ms 表示。

延迟分布用有效且因果关联明确的样本，输出 mean/P50/P90/P95/P99、n 和原始 per-case 数值。分位数固定 **nearest-rank**：升序数组 x、n 个值，`Q(p)=x[ceil(p*n)-1]`。超时样本为 right-censored，单独计数并显示响应成功率，不能悄悄从总样本中消失。每类仅 10 个样本时，P95/P99 基本就是最大值，报告必须显示这个限制。

结果状态分为 pass、fail、unknown、invalid、unsupported。模型在有效刺激后超时属 fail；网络/日志损坏属 infra_failed/invalid；刺激没在目标响应中触发属 invalid；不支持某能力的 case 在启动前 skipped_unsupported。缺少证据既不算成功也不捏造失败原因。

主报告同时给：已尝试数、有效数、各状态数、coverage、`case_success_rate = pass / attempted`（attempted 包含 invalid/infra_failed，排除未开始的 unsupported），以及 `valid_case_success_rate = pass / valid`。后者有效样本中保留 unknown。基础设施有问题时不得只展示后者；unsupported 不纳入已执行能力排行，但在计划样本覆盖率中显示。

## 2. Realtime 指标定义

以下 timestamp 全部在同一 `clock_id`，以纳秒差 / 1e6 转毫秒。客户端观测不等于服务器内部计算耗时。

### 2.1 Response Latency

对正常完整用户回合 u 及其首个有效音频响应 r：

```text
TTFA_receive_ms = (assistant_audio_start(r) - user_audio_end(u)) / 1e6
TTFA_playback_ms = (assistant_playback_start(r) - user_audio_end(u)) / 1e6
```

默认报告中的 **TTFA 指 TTFA_receive**，同时展示 TTFA_playback 与 playback_mode。assistant_audio_start 是首个有效非空 PCM chunk 到达时刻；“第一段可听语音”可另测 `time_to_first_speech`，需要固定 VAD/能量阈值，不能混为同一个指标。

`user_audio_end` 使用冻结音频标注中的语音结束边界映射到实际发送时间轴，不用文件末尾静音或 server speech_stopped 代替。manual commit 模式另报 commit→first_audio，不能与 server_vad TTFA 混合。提前响应得到负值时保留原值和 premature 标记，从 normal-response 非负分布中排除并单独统计；绝不截成零。

可观察的辅助区间：

| 区间 | 名称与限制 |
|---|---|
| vad_end_received − user_audio_end | client-observed endpointing delay，包含服务端 VAD、传输和排队 |
| response_start_received − vad_end_received | post-VAD response-start gap，不叫纯 LLM processing |
| first_audio_received − response_start_received | response-start-to-audio gap，包含服务端生成、传输和队列 |
| first_playback − first_audio_received | 本地解码/缓冲/播放启动延迟 |

中间事件缺失或先后顺序不成立则该分解 unavailable。没有服务端 profiling 时，不声称分出了纯 server VAD / model processing / audio generation 三项。Qwen SDK 内置 first_audio_delay 起点不同且用 wall clock，不采用它作为本项目 TTFA。

### 2.2 Interruption / Barge-in

典型刺激是助手正在讲北京，用户插入“等等，换成上海呢？”。主实验只注入音频，不根据已知 interruption 标签主动 cancel。

eligible 需要：旧 response 已开始播放、刺激确实在播放中送入、该控制 track 需要的服务端响应仍 active、存在足够后续播放/生成证据，以及日志完整。仅剩本地缓存播放、服务端早已完成的样本标 `playback_only`，不混入 native cancellation 统计。绑定目标 response 后，后来的上海回复不能当成“残余北京音频”。

| Metric | 定义 |
|---|---|
| Speech Detection Rate | 刺激后观测窗内匹配 vad_start 的 eligible 次数 / eligible 总数，仅作诊断 |
| Interruption Detection Rate | eligible 中有可靠 `interrupt_detected` 证据的次数 / eligible 总数；标明证据类型 |
| Stop Latency | `t_stop_old_response - t_interrupt_start`，主指标取旧 response 播放最后一个实际消费样本的末端 |
| Residual Audio Duration | 刺激开始到旧 response 最终停止期间，实际消费的旧响应 PCM 样本时长总和；包含样本内部静音，不包含没有消费旧样本的播放间隙 |
| Residual Speech Duration | 可选；对上述残余音频用固定 VAD 求语音时长，算法单列 |
| Context Switch Accuracy | eligible cases 中新回复正确使用新意图、未继续完成旧意图的次数 / eligible 总数；标语义判定来源 |

Stop Latency 与 Residual Audio Duration 不同：前者是末端的墙上经过时长（用单调钟测），后者是旧音频占用时长；中间有停顿或 underrun 时不相等。若旧响应在 deadline 前未停止，stop=null、censored=true、interruption outcome=fail，不能仅对成功停止的样本报告好看的延迟。

播放瞬间空队列不足以证明“停止”。最终停止要求有明确取消/终止证据，或事后检查完整 observation window 并证明旧响应未恢复；若只是自然完成或网络断流，不能判检测成功。推断结果附 `evidence_level=inferred`。取消确认的客户端接收延迟另报 `cancel_ack_latency`；不能把它等同于用户听到的 Stop Latency。

固定 profile 起点可取 `observation_window_ms=5000`、`stop_target_ms=500`、`detection_target_ms=1000`、`min_continuation_ms=800`，这些都是**待校准的方案阈值**，不是行业标准，也不承诺 Qwen 达到。调参使用开发集，冻结后才跑计分集。

若厂商无法直接暴露 interruption detection，则把确认数、推断数、unknown 分列。证据不完整时给保守下界 `confirmed/eligible`、上界 `(confirmed+unknown)/eligible`，不得用 speech_started 或客户端发出 cancel 填满检测成功率。

Context switch 的简单中文槽位可以规则核对，但原文本包含“北京”不必然失败，例如“好的，不看北京，我们改看上海”。先依据是否将新意图作为回答目标，规则不确定时交 judge/人工抽查。不可仅用“出现上海”判成功。

### 2.3 User Backchannel

刺激覆盖“嗯、嗯嗯、对、是、好的、哦、啊、我知道、你继续”等；选在有明确后续内容的助手语音中。语境应使其表示继续倾听，歧义用例单独分组。“好的”在某些任务中可能表示结束/确认，不能预设所有语境都为同一种行为。

```text
False Interruption Rate = false_interruptions / eligible_backchannels
Continuation Rate = continued_as_expected / eligible_backchannels
```

误打断证据为 backchannel 后助手将旧回答作为打断取消，或出现经规则确认的非自然持续中止/重启。短暂停顿、音频分块空隙、正常讲完均不是误打断。日志无明确取消且行为无法区分自然结束时记 unknown；给 FIR 下界 `false/eligible` 与上界 `(false+unknown)/eligible`，不强行记零。

同样保留 native_server、client_vad_flush、client_forced 三种控制 track。client_forced 只用于取消机制测试，不进入模型自然打断/反误打断主指标。官方 demo 若任何 speech_started 都清队列，就可能主要测到播放器政策，必须显式呈现。

clean 对照（无 backchannel 的同一主问题）用于校验场景与误判，属于额外 attempts，不混入 10 个 backchannel 的分母。非确定模型的 clean 回复不要求逐字/逐时一致，不能把另一次生成当严格反事实。记录实际可用的 continuation evidence。

### 2.4 Pause / Turn Taking / Overlap（MVP 2）

| Metric | 规则 |
|---|---|
| Premature Response Rate | 标注的非终止 pause 窗口内出现不应发生的 substantive response 的窗口数 / eligible pause 窗口数 |
| Turn Completion Accuracy | 完整请求结束后，回答满足完整意图且没有把半句当最终任务的次数 / eligible turn-taking cases |
| Overlap Response Appropriateness | 对 listener backchannel、side conversation、ambient speech、interruption、simultaneous speech 各自 oracle 判定，分类型报告 |

pause 分组为 200/500/800/1200/2000 ms，同文本、同说话人配对。记录任何 audio start 的原始 premature 候选与“实质抢答”的语义判定，允许合理 listener backchannel 的场景需预先声明，不能用英语 3 词阈值处理中文。语义判定与时序判定分开。

### 2.5 Case Success 与指标输出

- Latency pass：日志有效、完整输入后在 timeout 内有正确关联的非空音频、没有异常结束。TTFA 性能门槛如需设置在 profile 中明示，MVP 不凭空设排行及格线。
- Interruption pass：刺激有效、旧响应在 profile deadline 内停止、检测证据符合规则、新意图被正确响应。语义证据不足则 unknown。
- Backchannel pass：刺激有效、旧回答在规定观察窗内按语境继续、没有误取消/误重启。可观察性不足则 unknown。

示意 metric entry：

```json
{
  "name": "ttfa_receive_ms",
  "metric_version": "0.1",
  "value": null,
  "n": 0,
  "eligible_count": 1,
  "timeout_count": 1,
  "unknown_count": 0,
  "status": "no_valid_latency_samples",
  "evidence_event_ids": ["ev_user_end", "ev_timeout"]
}
```

这表示没有测到首音频，不代表 TTFA=0，也不是虚构的模型结果。

## 3. 音频资产与重复实验

MVP 使用少量人工预录和冻结 TTS。每项有音频 hash、原始/发送采样率、声道/位深、文本、speech bounds、speaker_id、来源/许可、语言、语速、变体关系。TTS 不能在计时路径内实时生成；资产需抽听确认中文内容、停顿和音量。

文本测试集先通过独立 renderer 编译，整套音频完成后才允许打开 target session。缓存键固定文本、TTS profile、renderer/decoder 指纹与渲染 recipe；runtime Scenario 另用编译器源码指纹寻址。这样编译逻辑升级会产生新 Scenario，又不会因无关代码变化重复调用收费 TTS。每次 run 保存 compilation manifest hash，每个 case 保存实际输入 WAV 和 renderer profile。格式见 [文本测试集说明](text-dataset.md)。

MVP 1 默认 clean、无声学回声，输入直送 API，不把助手输出再混回用户音轨。这样测完整语音会话服务与客户端控制，不声称覆盖麦克风/扬声器/AEC 真实设备链路。设备回环实验另建 profile。

发送使用实际采样数和 monotonic deadline，默认建议 20 ms；记录 pacing error 分布、最大迟到、transport 耗时和缓冲水位。是否达标的 tolerance 在预试验后冻结；不根据结果临时改变。

后续 human speech 分层包含不同 speaker、语速、口音、口头语、hesitation、self correction、false start。clean/20/10/5/0 dB SNR 与 office/cafe/car/metro/music/background speech 作为变体，不提前实现全矩阵。SNR 明确采用 active-speech RMS 或整段 RMS 的哪一种，固定 noise 截取、gain、限幅策略并保存处理后音频，不能只保存“10dB”标签。

## 4. 评价策略与质量维度

优先级是 Deterministic State → Rule-based → LLM Judge → Human Sample。不同方法解决不同问题：数据库写对由 state oracle 决定；声音自然度不能靠数据库证明。

| 维度 | 首选证据/方法 |
|---|---|
| Instruction Following | 可验证约束用规则；开放要求用固定 rubric |
| Knowledge / Reasoning | 冻结题目与参考答案、精确/等价规则；开放题 judge，注明资料日期 |
| Multi-turn Consistency | 历史事实/槽位一致性规则 + judge |
| Speech Understanding | 输入真人/冻结音频与人工参考标注，实体准确率、中文 CER 作诊断 |
| Chinese Expression | 中文口语 rubric + 人工抽听 |
| Conciseness / Redundancy | 回答时长、冗余片段/重复诊断 + 语境适当性 judge |
| Naturalness | 音频 judge 或人工听辨；单靠文字不可完整评价 |
| Conversational / Spoken Response Appropriateness | 信息密度、轮次长度、必要确认、可听懂的数字和列表；不奖励无条件长答案 |
| Robustness | 与 clean 同内容配对，分 speaker/environment/content 展示变化 |

Judge 保存 model/revision、prompt/rubric version、temperature、输入/输出和解析状态；隐藏被评模型名，禁止让 judge 改写工具状态结论。冻结的语音转写也有版本与 hash；ASR 误差大时暂停内容判定、抽听，不能归罪模型。

Human Sample 在质量阶段每类抽样，并增补 judge 分歧/低置信度 case；预先固定抽样 seed。报告人际一致性、judge-human agreement（分类用 Cohen's kappa，序数可加 weighted kappa）、分歧例子。样本不足不宣称 judge 已可靠。MVP 1 不引入通用多模型 judge 平台，仅为 context switch 的必要语义验证预留可选入口。

## 5. Agent 阶段设计

### 5.1 Deterministic Mock Tool Server

工具只从 weather、train、calendar 开始，建议最小函数是 `get_weather(city,date)`、`search_train(from_city,to_city,date,time_window)`、`create_calendar_event(...)` / `update_calendar_event(...)` / `list_calendar_events(...)`。具体函数 schema 在 Phase 7 才实现，不提前添加 flight/hotel/restaurant。

每个 attempt 装载同一 fixture 的全新状态与固定世界时间。例如 `2026-09-19 10:00 Asia/Shanghai` 下“明天”解析为 `2026-09-20`；不使用机器今天。城市、日期、时区与枚举按显式规则归一化，禁止静默把错误城市修成期望城市。

查询结果固定，写入结果持久化到 mock DB。event ID/业务 ID 由 fixture+操作序号确定；工具延迟/failure schedule 固定、无未设 seed 的 random。写入工具使用显式幂等键与一次提交语义，保存 before/after hash 和变更 diff。运行结束即使失败，也保存初始/最终状态及审计。

### 5.2 场景类型与 correction 竞态

| 类型 | 最小例子 | 主要 oracle |
|---|---|---|
| Single Tool | 查上海明天天气 | 正确函数/参数/结果引用 |
| Multi Tool | 比较北京和上海天气，再查高铁 | 必需调用集合、无错误额外调用 |
| Multi Step | 查车次后按结果建立日历 | dependency DAG、结果引用、最终状态 |
| Correction | 上海→北京改为上海→天津 | 参数更新、禁止旧意图写入、最终任务目标 |
| Tool Failure | 日历写入失败后重试或说明失败 | 故障触发、允许恢复路径、未虚报成功 |
| Ambiguous Request | 只说“帮我安排明天下午” | 未擅自写入、提出必要澄清；先不实现自由 bot 用户 |

Correction 用例明确相对边界：工具调用前、调用已开始但未完成、可逆写入后。注入触发以 `tool_execution_start` 等事件绑定 call_id，固定延迟给用户说完修正的机会。

评价分开检查：修正前合法的北京查询不因事后改变意图被追溯判错；修正后的动作必须使用天津；过期的北京结果可以留在审计，但不能作为最终意图结果继续写入。若旧日历写入已完成，只在场景预设可逆时要求修改/补偿；若工具不支持取消，不能要求模型执行不存在的 cancel 协议。模型必须自己作出修正，benchmark 不依据隐藏 oracle 自动撤销错误操作。

### 5.3 Failure fixtures

| 故障 | 固定语义 | 可以接受的恢复，按 case 预先声明 |
|---|---|---|
| Timeout | 区分未执行即超时与已提交但响应迟到 | 有界重试/查询状态；写入带幂等键 |
| HTTP 500 | transport error，默认没有状态提交 | 有界 retry、明确失败或替代方案 |
| No Result | 成功执行但结果为空 | 澄清、改条件或说明无结果 |
| Permission Denied | 不允许写入，无副作用 | 请求适当授权/说明受限，不重复盲试 |
| Invalid Argument | 类型或业务校验失败，无副作用 | 修正参数或询问用户 |

不是每种故障都应 retry，failure recovery 以场景允许的最终状态/行为为准。请求超时但实际已经提交的 fixture 必须真实保留状态，不能默认所有 timeout 都没发生副作用。

### 5.4 Agent Metrics

| Metric | 定义与分母 |
|---|---|
| Tool Selection Accuracy | eligible Agent cases 中，必需工具集合、调用次数约束与禁止工具均满足的比例；额外报调用级 precision/recall |
| Argument Accuracy | 对齐 oracle 预期调用实例后，全部 typed 参数约束满足的实例数 / 预期必需实例数；缺失计错，多余非法参数计错，额外调用另报 |
| Tool Call Sequence Accuracy | 满足允许顺序或 dependency DAG、结果依赖及重试规则的 cases / eligible sequence cases |
| Task Completion Rate | 状态目标及任务证据全部满足的 cases / eligible task cases；写入任务核对 DB，查询任务还核对正确调用/结果与答案 |
| Correction Handling Accuracy | 修正后的正确参数/最终状态、过期结果处理、禁止副作用均满足 / eligible correction cases |
| Failure Recovery Rate | 故障已实际触发且按允许恢复路径结束 / eligible injected-failure cases |
| Hallucinated Action Rate | 至少一次声称操作完成而调用结果/提交状态不支持的 cases / eligible action cases；附 claim 判定覆盖率 |

参数匹配支持显式 alias/数值容差/时区规则，不做不受约束的模糊匹配；`$RESULT` 式依赖必须解析为实际前序结果再验证。多个正确执行路径用 DAG/约束表示，不因合理独立调用交换顺序扣分。

Task Completion 优先采用 EVA 的规范化状态比较与 diff 思想，但不会在 read-only 任务上以“初始=最终”直接判成功。状态正确而用户被告知失败，应额外反映 communication correctness；声称成功而状态不对，不能因为自然语言听起来可信判完成。

自然语言“已经完成”的 claim 提取未必完全 deterministic。规范化结构化声明/明确模板可规则判，开放中文需要 judge/人工；判定须附 claim span、tool result、state evidence。未知 claim 不自动算无幻觉，报告 confirmed 与 unknown。

## 6. 实施顺序与阶段退出条件

用户已批准设计，Phase 1/2 的契约与真实接入，以及 Phase 3 的单轮 latency runner/evaluator 已完成。Phase 4 已补失败路径回归和完整工件；Phase 5 Backchannel 与 Phase 6 duplex 指标已接入共享事件契约；Phase 7 Mock Tools、Phase 8 Agent evaluator 已可离线运行。Step/Doubao/本地 Omni 适配器保持未核验并拒绝伪造 live protocol，不改变 Qwen → Step → Doubao 顺序。

| Phase | 产物 | 验收与必要检查 |
|---|---|---|
| 1（已完成） | Event Schema/Recorder、Scenario Schema、Adapter Base；Python 3.11 + uv.lock | schema roundtrip、时钟边界、raw/normalized/PCM 引用、partial artifact；scripted transport 可录可重放，无模型分数 |
| 2（已完成） | Qwen Realtime Adapter + connection probe | manual/VAD 真实音频往返及主动取消确认、双向事件/PCM/WAV/配置记录；不等于正式 latency/barge-in 测量 |
| 3（已完成） | Latency runner/evaluator、10 条冻结 TTS、后台录制与虚拟播放 | 真实 10-case 运行；语音边界来源分组、timeout/提前响应/计时超限反例、离线重算一致；见 [实测记录](latency-implementation.md) |
| 4（基础链路已验收，继续扩展） | 双轮 runner、evaluator 0.3、1 个 v2 中文 fixture、25 项专用回归和完整 Qwen 工件 | 扩展不同内容场景、校准语义规则；unknown 不填为成功。见 [Phase 4 实现](interruption-implementation.md) |
| 5（基础实现） | Backchannel runner/evaluator、synthetic fixture、一次 Qwen 协议实验 | 不读 oracle 控制 cancel，continued/false/unknown 分离；扩展真实中文语料和多 case |
| 6（报告） | 基础 HTML report、CLI 工件导出 | 离线可生成报告，继续扩展可视化 |
| MVP 2 扩展（时序闭环） | 显式输入区间、9 个衍生场景、duplex-0.2 | 5 个真实 pause 变体已跑；Overlap 内容理解仍 unknown，需扩展多人/自然语料 |
| 7（基础实现） | Mock Tool Server | 三领域、隔离初态、严格参数、call_id 幂等、固定故障、状态 hash 和统一事件；待扩展超时后已提交故障 |
| 8（本地闭环） | 音频 → Qwen工具事件 → runtime → 结果回传 → 音频 → 状态重放 | 标准答案不参与执行；fake 协议与工件回归通过，Plus 天气、查车→日历和查询执行中改口有真实工件；支持有序结果依赖，允许多拓扑 DAG 和歧义追问仍待扩展。见 [Agent 实现](agent-implementation.md) |
| 9 | Step Adapter | 当时官方协议验证、同一契约与基准；先能力 probe |
| 10 | Doubao Adapter | 同上，不提前猜 endpoint、鉴权或环境变量 |
| 11 | Qwen 开源 Omni Adapter | 独立后端版本/GPU/streaming profile，不能伪造 full duplex |

每 phase 只跑与改动相关的验证。基础设施测试使用必要的失败/竞态反例：无首音频、负 TTFA、旧音频晚到、重复调用、timeout 已提交、backchannel 被客户端清队列、日志截断。真实模型 smoke test 留独立 marker，需显式配置凭据与预算，默认测试不联网收费。

## 7. 第一版 Definition of Done

下列命令已支持单轮 latency；完整 MVP 1 仍需 Phase 4–6 的 interruption/backchannel/report：

```bash
python -m benchmark.run --model qwen-realtime --suite realtime --scenario scenarios/realtime/basic.yaml
python -m benchmark.evaluate --run runs/<run_id>
```

当前 `basic.yaml` 包含 10 条 latency case；后续再加入 interruption/backchannel。未运行维度不填零，Latency case 不产生另外两类指标。当前每个 case 的源工件在 `cases/` 下，聚合 metrics 与版本化重评在 run 根目录及 `evaluations/`。

Done 要同时满足：

1. 实际 Qwen 会话完成 audio in → streaming audio out，不是只有 mock/replay 成功。
2. 保存 config、输入与输出 WAV、双向 raw events、normalized events、transcript、metrics、HTML；可选工具文件在无工具场景无需伪造。
3. 报告 TTFA P50/P95、Interruption Detection Rate、Stop Latency P50/P95、False Interruption Rate、Case Success Rate，并展示样本数、失败/unknown/invalid 和控制策略。其余 P90/P99/mean 一并提供。
4. 原始音频与事件可 replay，离线重复评价无需重新调用模型，指标值一致。
5. 三类刺激实际触发，音频停止/继续具有可解释证据；若模型本身表现差，低分是有效结果，但缺失核心测量能力不能靠全部 null 宣布 Done。
6. 固定测试集重复运行，记录每轮配置和波动；异常退出可保留 partial artifacts，报告不把缺失当零。

## 8. 已确认的设计决定

已确认采用：Python/asyncio、统一双时钟事件、收到音频与播放音频分离、MVP paced virtual playback、native/server 与客户端强制控制分 track、离线评价、遵循 Qwen 官方协议的 adapter、EVA 式状态 oracle。Qwen transport 采用原设计允许的 asyncio WebSocket 路线。Phase 2 已验证一个账户/模型/音色及客户端取消；自然打断、上下文恢复和阈值仍属于后续实验。

Phase 2 probe 的接收 WAV 和 file-end 诊断仍不进入正式 latency 统计。Phase 3 已实现冻结边界、播放时间线、发送/写入解耦和调度门槛；当前真实数据使用自动能量边界，TTFA 明确属于该估计边界组。人工边界与真人语音覆盖尚待补充。
