# 项目交接与开发约定

状态核对日期：2026-09-21。阶段事实以代码、测试和工件为准；设计文档中的目标接口不自动代表已实现。

## 阶段状态

- Phase 1–3 有基础契约、Qwen 接入及 latency 的历史实现/实测。Phase 3 真实运行保存在 `runs/qwen-phase3-latency-001/`，输入边界是自动估计。
- **Phase 4 基础链路与主要失败路径已回归，多内容扩展未完成。** 固定双轮 runner、evaluator `interruption-0.3`、25 项专用 interruption 测试和场景 v2 已具备。真实完整工件 `runs/qwen-phase4-interruption-20260919-001/` 的检测 confirmed、stop 371.013ms、residual 355.655ms；语义规则因为澄清时提及旧城市而为 unknown。
- 历史 `runs/qwen-phase4-interruption-20260918-005/` 的新回答播放被截断。0.2 的 pass 过宽，0.3 重评为 unknown；原始工件及旧 evaluations 不改写。不要沿用“1/1 pass 等于完整中断验收”的结论。
- Phase 5 Backchannel 已有独立 runner/evaluator、合成 fixture 和本地回归；duplex-0.2 已加入显式 input_region 边界与9个冻结衍生场景，5-case真实pause结果1pass/3fail/1invalid。中文“嗯嗯”“你继续”TTS已准备，Agent天气工具已真实跑通；已有查询执行中改口与有序结果依赖；后续继续多用例、Overlap内容评价、歧义追问与多拓扑DAG。HTML report、Mock Tools、Agent 已有基础实现；Step、Doubao、本地 Omni 仅有 deferred adapter，必须先核验官方协议才能 live。
- 2026-09-21 全量 155 项测试通过，Ruff 通过。异步磁盘测试在受控执行环境运行，普通沙箱可能卡在 asyncio 线程唤醒。指标 unknown 与 invalid 分开；case_abort 不能填入 Stop Latency。
- `minimum_continuation_evidence` 使用本地已收到但未播放的 PCM，在语音实际开始处检查；缺少足够缓冲为 invalid。混合 category 的 suite 在任何外部调用前拒绝。完整定义见 [docs/interruption-implementation.md](docs/interruption-implementation.md)。

- Phase 7/8 已移除按 expected_calls 执行答案的假运行。Agent runtime 只执行 Adapter 实际 tool_call_end，Qwen 已实现完整调用映射/批量结果注入/继续生成，依据固定官方例子，Plus模型真实天气工具闭环在 `runs/qwen-agent-weather-20260920-001/`，task_completion=true。agent.evaluate 校验封口并从实际调用重放最终状态。详见 [docs/agent-implementation.md](docs/agent-implementation.md)。
- assets extra 下载曾超时，现可用官方协议核验的 `--transport stdlib` 生成 TTS；新 Agent 和 Backchannel WAV 已冻结。旧无音频 Agent YAML 仍仅为 oracle 模板，CLI 必须拒绝，不补造调用。

- 中文 Backchannel 两轮共4次尝试保留在 `runs/qwen-backchannel-tts-20260920-001/` 和 `002/`：3 invalid，1 eligible/fail（“你继续”触发 server cancel）。不能宣称中文 Backchannel 已通过或删除无效样本；旧音调实验单列。

- 本轮额外实测：`qwen-turn-taking-20260920-001` 1 pass；`qwen-overlap-20260920-001` 3 eligible（1 timeout fail、2 内容 unknown）。6个新 run 的完整性、密钥扫描、两次重评一致性通过，源工件未改变。

- 新入口 `benchmark.run --suite agent` 支持 suite/warmup/repetition，逐case封口并离线重放。建议运行 `scenarios/agent/advanced_qwen.yaml`；`advanced.yaml` 保留原20ms长输入实验。
- `qwen-agent-advanced-20260920-001`：改口pass，HTTP500未重试为fail，多步骤因输入背压invalid；`qwen-agent-multistep-20260920-002` 同配置重复仍invalid。`qwen-agent-multistep-200ms-20260920-001` 同音频200ms分帧pass，真实查天津G2后创建模拟日历，原20ms调度门槛不变。不可把输入分帧变化隐藏或删掉失败记录。
- Agent evaluator 0.4 校验结果引用及时序依赖，并新增首调用函数/参数准确率及重复同调用诊断；提前猜对参数不能通过。Correction 通过语音开始时工具仍pending的证据验证；旧查询继续记录，benchmark不偷改参数。复杂写入补偿/歧义追问未完成。

- 文本优先输入链路已实现：`dataset/` 校验紧凑 corpus/详细 scenario，`renderers/` 在 target session 前调用 Qwen TTS，冻结内容寻址 WAV/metadata，再编译为现有 Scenario。半双工、全双工 interruption、显式 800ms pause 均有真实 Qwen 链路工件；完整格式见 `docs/text-dataset.md`。
- 紧凑半双工 corpus 的真实工件为 `runs/qwen-text-corpus-half-20260921-001/`，2/2 pass，receive TTFA P50 1200.236ms、P95 1228.364ms，输入均为自动能量边界。这只是基础设施验收，不代表总体性能。运行复用了2条缓存，`provider_calls=0`；离线重评 evaluation_id 不变。
- 紧凑全双工 corpus 的真实工件为 `runs/qwen-text-corpus-full-20260921-001/`：eligible=1，confirmed interruption，Stop Latency 381.465ms，Residual Audio 362.878ms；新回答 completion timeout，因此 case 为 fail、context switch 为 unknown。运行复用2条缓存且 `provider_calls=0`，离线重评 ID 不变。
- 文本输入缓存身份只包含文本、TTS profile、renderer/decoder 与渲染 recipe；完整 compiler/schema 指纹用于 Scenario compilation ID。正式对比先冻结音频，再去掉 `--render-missing`，避免运行时意外调用收费 TTS。
- `dataset.cockpit` 已支持导入 `common_func_100.jsonl` 和 `白名单_100.jsonl`，转换100个函数为严格 JSON Schema，按一基行号或 `start-line + limit` 生成半双工 Agent suite。20,169条源数据中20,029条有工具、140条no-tool；no-tool必须另建 False Tool Call Rate suite。
- 全量源审计 `reports/cockpit-source-audit-20260921.json` 显示19,551条工具样本符合协议、478条存在期望参数类型/形状冲突。编译器拒绝冲突行，不做静默类型转换；批量运行前必须修源数据或建立有版本的显式 normalization。
- Audio 3.0 Flash 配置使用 `longanqian`、smart-turn、2400ms尾静音及200ms输入分帧。真实 smoke `runs/qwen-audio3-cockpit-smoke-20260921-001/` 的5条均completed/eligible；首调用函数和参数均5/5正确，但5/5各重复一次相同调用，严格 Task Completion为0/5。离线重评为 `eval_dce531cd2a086d329206`。完整说明见 `docs/cockpit-benchmark.md`。
- Audio 3.0 调通过程中的 probe `001`–`017` 不删除：`001`配置回显过严，`002/004/005`话轮未生成，`003`发送调度invalid，`007`起暴露 response/tool续答竞态，后续记录重复调用策略实验。它们是协议适配证据，不能改写为统一的模型失败样本或成功样本。

## 工程边界

- Python 3.11 + uv.lock；厂商 wire protocol 留在 `adapters/`，runner/evaluator 不调用厂商 SDK。
- 新实验使用独立 run/attempt 目录，保留失败；原始事件、音频和历史评价不可因修复而改写。离线重评使用独立 evaluation_id。
- Latency/interruption/backchannel、自动/人工边界、native/server 与 client-forced 控制策略分组，不合成 Overall Score。
- API key 由进程环境获取，不写源码、配置、日志、报告或命令行实参。默认测试不得调用收费模型服务。
- root `.git` 在此环境是无效只读占位目录；不要声称 git clean 或擅自修复 Git 元数据。

## 本地检查

```bash
uv sync --locked --python 3.11 --extra qwen
.venv/bin/ruff check .
.venv/bin/pytest -q
```

环境中的普通沙箱曾阻塞 asyncio 线程退出；这与 Qwen 模型错误不同。若受控执行被审批服务拒绝，应准确记录未执行的测试/请求，不绕过拒绝，也不把收集数量写成通过数量。纯内存反例和无需线程的单元测试可先做。

项目状态与使用方法见 [README.md](README.md)；不将项目细节写入全局 agent 配置。
