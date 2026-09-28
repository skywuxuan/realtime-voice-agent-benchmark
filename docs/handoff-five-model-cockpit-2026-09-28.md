# 五模型智能座舱 Benchmark 交接

状态核对日期：2026-09-28。本文是本地跑测与排查的详细入口，供后续 agent 继续分析。
代码能力以当前工作树为准，最终统计以五个 provider archive 和 sealed artifact 为准。
不要从目录名或未封口 run 猜结果。

## 1. 五分钟入口

1. 先打开五模型 HTML：
   `reports/cockpit-3000-five-model-20260927/index.html`。
2. 总体结构化结果：
   `reports/cockpit-3000-five-model-20260927/summary.json`。
3. 每个源行、每个模型的 ASR、实际调用、延时和最终工件位置：
   `reports/cockpit-3000-five-model-20260927/cases.json`。
4. 疑似 ASR 相关 Fail：
   `reports/cockpit-3000-five-model-20260927/asr_failures.json`。
5. 五个模型的正式结果归档：
   `reports/cockpit-3000-five-model-20260927/sources/`。
6. 生成器：`reports/cockpit_five_comparison.py`。

`cases.json` 是逐条索引。按 `source_line_number` 定位输入，每个 `providers.<name>` 下都有：

- `status`、`eligible`、`failure_category`、`execution_reason`
- `asr_text`、`asr_evaluation`
- `calls`、`first_call_argument_diff`
- `timing.speech_end_to_final_tool_call_ms`
- `timing.speech_end_to_first_tts_frame_ms`
- `artifact_path`、`evaluation_id`

不要把 Markdown 扩成 2893 行静态清单；逐条事实必须从这个 JSON 和 sealed artifact 读取。

## 2. 统一测试口径

### 2.1 数据与工具

- 工具协议源：
  `/mnt/lustre/hpc_stor01/home/xumao.wu/src/2026/aaa_tmp/TSL_use/protocol/common_func_100.jsonl`
- case 源：
  `/mnt/lustre/hpc_stor01/home/xumao.wu/src/2026/aaa_tmp/TSL_use/testset/白名单_100.jsonl`
- 转换 manifest：
  `datasets/cockpit/converted/cockpit_common_func_100_whitelist_v1/7c559fe0e0c851756081/manifest.json`
- 可读源行索引：同目录 `cases.json`
- 共享 prompt：同目录 `prompt.json`
- 工具 catalog：`datasets/catalogs/cockpit/cockpit_common_func_100_795b4022d6ab.json`
- 16 kHz 冻结 TTS：`datasets/rendered/qwen3_tts_flash_cherry_16k_v1/`
- Step 24 kHz 派生音频：
  `datasets/rendered/qwen3_tts_flash_cherry_16k_v1_resampled_24000_v1/`

源行 1–3000 共 3000 行：2893 条 valid-tool、58 条标签/schema 冲突、49 条 no-tool。
Task Completion 只评价 2893 条 valid-tool。Step 只跑源行 1–200，其中 193 条 valid-tool、
5 条标签/schema 冲突、2 条 no-tool。

### 2.2 拓扑与 Prompt

所有正式对比均为 `expected_tool_only`：每条 case 只暴露标签对应的一个工具 schema。
因此函数名正确率不代表从 100 个函数中做路由选择的能力；主要评价严格参数抽取和工具闭环。

模型公开上下文只包含：

- `dataset.cockpit.COCKPIT_SYSTEM_PROMPT`
- 固定场景时间 `未指定`
- 当前 case 的一个工具 schema

`expected_calls`、标签参数和最终状态不进入模型上下文。每条实际 session prompt 位于 attempt 的
`session_config.json`；compact/bundle 工件可通过后文方法恢复或直接从聚合索引读取。

### 2.3 最终汇总

| 模型 | 覆盖源行 | 总数 | Eligible | Pass | Fail | Invalid | Eligible 通过率 | ASR字准 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Qwen Audio 3.0 Realtime Flash | 1–3000 | 2893 | 2887 | 2434 | 453 | 6 | 84.3% | 97.5% |
| Qwen Audio 3.0 Realtime Plus | 1–3000 | 2893 | 2893 | 2448 | 445 | 0 | 84.6% | 97.5% |
| Qwen Audio 3.1 Realtime Plus | 1–3000 | 2893 | 2893 | 2492 | 401 | 0 | 86.1% | 97.4% |
| Seed Duplex 3.0 | 1–3000 | 2893 | 2893 | 2380 | 513 | 0 | 82.3% | 94.4% |
| StepAudio 3 Realtime | 1–200 | 193 | 176 | 94 | 82 | 17 | 53.4% | 94.3% |

Qwen 3.1 最初有 45 条最终 invalid。2026-09-27 运行第三轮 invalid-only retry 后，45 条全部
变成 eligible，其中 38 条 Pass、7 条模型 Fail，最终 Invalid 为 0。旧 invalid attempt 仍在
工件和 `attempt_count` 中，最终结果只选每个 case 的首个 eligible attempt。

### 2.4 延时

延时起点统一为 `user_audio_end`。Function Call 终点是最终 `tool_call_end`，TTS 首帧终点是
首个 `assistant_audio_start`。以下为原始观测统计，没有删除长尾点：

| 模型 | Function Call P50 / P95 | TTS 首帧 P50 / P95 |
|---|---:|---:|
| Qwen 3.0 Flash | 1089.682 / 11799.517 ms | 2042.501 / 13350.353 ms |
| Qwen 3.0 Plus | 1518.245 / 6261.309 ms | 2627.905 / 7446.060 ms |
| Qwen 3.1 Plus | 6406.275 / 24067.809 ms | 7905.052 / 25815.184 ms |
| Seed Duplex 3.0 | 1517.076 / 1974.996 ms | 2617.510 / 3354.007 ms |
| StepAudio 3 | 2190.677 / 6069.249 ms | 2288.669 / 5165.105 ms |

Qwen 3.1 的延时显著高于另外四个模型，且运行中连接/控制错误较多，是后续排查的最高优先级之一。

## 3. ASR 口径

ASR 对比前，参考文本和 API 中间 transcript 都批量通过：

```text
python ~/tools/ezmt-patslot-train_latest/norm.py input.txt output.txt
```

实际脚本：`/mnt/lustre/hpc_stor01/home/xumao.wu/tools/ezmt-patslot-train_latest/norm.py`，当前文件
SHA-256 为 `7575a6ddc8b66d755bb279d71b52d58413215f6756f924c40e8091573f6d2ca6`。
生成器在可写临时目录运行它，并把脚本目录放入 `PYTHONPATH`。不要直接在脚本目录运行，
`NormText` 会尝试写 `norm_error_sentence.log`；仓库 `.venv` 缺少其 `chardet` 依赖，生成器使用
当前 `PATH` 中可运行该脚本的 `python`。

`ASR字准 = 1 - micro CER`。标点、空格、大小写由后续规范化移除；阿拉伯数字等由上述脚本
转换。中间 transcript 是 API 的可观测辅助输出，不一定是音频模型执行工具时唯一使用的内部
表示，因此 `ASR 不一致 + Fail` 只标为疑似相关，不宣称因果。

| 模型 | ASR字准 | 逐字完全一致率 | Fail 且 ASR 不一致 | Fail 且 ASR 一致 | Fail 缺 ASR |
|---|---:|---:|---:|---:|---:|
| Qwen 3.0 Flash | 97.5% | 84.5% | 69 | 370 | 14 |
| Qwen 3.0 Plus | 97.5% | 84.5% | 64 | 361 | 20 |
| Qwen 3.1 Plus | 97.4% | 84.3% | 58 | 322 | 21 |
| Seed Duplex 3.0 | 94.4% | 71.4% | 122 | 382 | 9 |
| StepAudio 3 | 94.3% | 65.6% | 35 | 47 | 0 |

## 4. 每个模型的正式工件

### 4.1 Qwen Audio 3.0 Realtime Flash

- Adapter：`qwen-realtime`
- 配置：`configs/qwen-audio-3.0-realtime-flash-agent.yaml`
- endpoint：`wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen-audio-3.0-realtime-flash`
- 输入/输出：16 kHz / 24 kHz mono PCM16
- turn detection：`smart_turn`
- 凭据环境变量：`DASHSCOPE_API_KEY`
- 正式结果归档：
  `reports/cockpit-3000-five-model-20260927/sources/qwen-audio3-realtime-flash.json`
- 最终 bundle：`runs/bundles/qwen-flash-3000-20260924/`

归档严格限定源行 1–3000，包含 2893 个 valid-tool case。正式 bundle 中包含71个源 run、
3139个历史 attempts；每个源 run 的精确名称在 `bundle.json.source_runs`，每条压缩结果在
`results.json`，原始证据在 `evidence.tar`。

主要失败：argument mismatch 434、no tool call 2、response timeout 14、other 3、invalid 6。

### 4.2 Qwen Audio 3.0 Realtime Plus

- Adapter：`qwen-realtime`
- 配置：`configs/qwen-audio-3.0-realtime-plus-agent.yaml`
- endpoint：`wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen-audio-3.0-realtime-plus`
- 输入/输出：16 kHz / 24 kHz mono PCM16
- turn detection：`smart_turn`
- 正式结果归档：
  `reports/cockpit-3000-five-model-20260927/sources/qwen-audio3-realtime-plus.json`
- 最终 bundle：`runs/bundles/qwen-plus-3000-20260924/`

bundle 中包含 60 个源 run、2997 个历史 attempts；精确 run 名称见 `bundle.json.source_runs`。
主要失败：argument mismatch 355、no tool call 68、response timeout 20、other 2。

### 4.3 Qwen Audio 3.1 Realtime Plus

- Adapter：`qwen-realtime`
- 配置：`configs/qwen-audio-3.1-realtime-plus-agent.yaml`
- endpoint：`wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen-audio-3.1-realtime-plus`
- 输入/输出：16 kHz / 24 kHz mono PCM16
- turn detection：`smart_turn`
- 正式结果归档：
  `reports/cockpit-3000-five-model-20260927/sources/qwen-audio31-realtime-plus.json`

`runs/qwen-audio31-plus-cockpit-full-000001-000500-20260924-main-001` 是 watchdog 重启前留下的
未封口旧 run，不在正式归档的 `runs` 列表中，禁止计入最终统计。归档引用的24个 run 都是
compact run，精确列表见归档的 `runs` 字段，证据位于各目录 `evidence.tar.gz`，摘要位于
`results.json`。

主要失败：argument mismatch 350、no tool call 25、response timeout 21、other 5。最终 Invalid 0。

### 4.4 Seed Duplex 3.0

- Adapter：`doubao-realtime`
- 配置：`configs/doubao-realtime.yaml`
- latency profile：`configs/latency-seed-duplex3-cockpit-tolerant.yaml`
- endpoint：`wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue`
- 服务模型版本：`1.2.6.1`
- 输入/输出：16 kHz / 24 kHz mono PCM16
- turn mode：server VAD
- 凭据环境变量：`BYTEDANCE_LLM_API_KEY`
- 正式结果归档：
  `reports/cockpit-3000-five-model-20260927/sources/seed-duplex3.json`
- 最终 bundle：`runs/bundles/seed-3000-20260924/`

正式归档严格限定已封口的前3000行。bundle 中包含42个源 run、2910个历史 attempts；精确 run 名称见
`bundle.json.source_runs`。主要失败：argument mismatch 431、no tool call 66、response timeout
15、other 1。

### 4.5 StepAudio 3 Realtime

- Adapter：`step-realtime`
- 配置：`configs/step-realtime.yaml`
- endpoint：`wss://api.stepfun.com/v1/realtime?model=stepaudio-3-realtime-preview`
- 输入/输出：24 kHz / 24 kHz mono PCM16
- 正式 turn mode：manual commit
- 凭据环境变量：`STEPFUN_API_KEY`
- 正式结果归档：
  `reports/cockpit-3000-five-model-20260927/sources/stepaudio3-realtime.json`
- 正式 run：`runs/stepaudio3-manual-200-cockpit-full-000001-000200-20260924-main-002`

正式 run 已 compact，193 个 valid-tool attempt 中 176 eligible、94 Pass、82 Fail、17 Invalid。
主要失败：no tool call 50、argument mismatch 32；17 条基础设施 invalid 单列。

当前 runner 通过 `adapter.tool_dispatch_policy()` 表达差异：Qwen/Seed 默认在 response end 后执行；
Step 在 `tool_call_end` 后执行并允许原 response 完成前回注结果。不要把 Step 分支硬编码回 runner。

## 5. 如何定位单条证据

### 5.1 从源行定位

```bash
python - <<'PY'
import json
line = 1
cases = json.load(open('reports/cockpit-3000-five-model-20260927/cases.json'))
case = next(row for row in cases if row['source_line_number'] == line)
print(json.dumps(case, ensure_ascii=False, indent=2))
PY
```

查看 `providers.<provider>.artifact_path`：

- `runs/bundles/.../evidence.tar#<run>/<case>/<attempt>` 是 campaign bundle 成员。
- `runs/<compact-run>/evidence.tar.gz#cases/<case>/<attempt>` 是 compact run 成员。
- 松散路径直接指向 attempt 目录。

### 5.2 恢复 compact run

```bash
.venv/bin/python -m agent.artifacts restore runs/<compact-run>
```

Bundle 的原 run 列表、run ID 和 archive 前缀在 `bundle.json.source_runs`。不要直接解压后修改；
恢复和深度校验使用 `agent.artifacts` / `scripts.bundle_campaign` 的既有接口。

### 5.3 Attempt 文件

一个完整 attempt 的关键文件：

- `scenario.json`：输入、标签、音频引用、prompt、工具范围
- `session_config.json`：实际请求与服务回显
- `raw_events.jsonl`：厂商 wire event
- `events.jsonl`：统一事件，延时和因果证据来源
- `transcript.json`：输入 ASR 与助手转写
- `tool_calls.json`：实际执行的函数、参数、结果
- `trial.json`：执行状态与 backend 诊断
- `metrics.json`：该 attempt 离线评价
- `manifest.json`：文件哈希与封口状态

## 6. 报告生成

五模型报告生成命令：

```bash
.venv/bin/python reports/cockpit_five_comparison.py \
  --conversion-manifest datasets/cockpit/converted/cockpit_common_func_100_whitelist_v1/7c559fe0e0c851756081/manifest.json \
  --flash-report reports/cockpit-3000-five-model-20260927/sources/qwen-audio3-realtime-flash.json \
  --plus-report reports/cockpit-3000-five-model-20260927/sources/qwen-audio3-realtime-plus.json \
  --plus31-report reports/cockpit-3000-five-model-20260927/sources/qwen-audio31-realtime-plus.json \
  --seed-report reports/cockpit-3000-five-model-20260927/sources/seed-duplex3.json \
  --step-report reports/cockpit-3000-five-model-20260927/sources/stepaudio3-realtime.json \
  --text-normalizer /mnt/lustre/hpc_stor01/home/xumao.wu/tools/ezmt-patslot-train_latest/norm.py \
  --max-line 3000 \
  --output reports/cockpit-3000-five-model-20260927
```

输出文件：`index.html`、`summary.json`、`functions.json`、`cases.json`、
`asr_failures.json`、`README.md`。`sources/` 中五个归档是输入，不会被生成器改写。
页面顶部是5行总体表；逐 case 可按模型、状态和 ASR 归因筛选。

## 7. 后续排查优先级

1. **Qwen 3.1 延时**：准确率最高，但 Function Call/TTS P50 分别约 6.4s/7.9s，P95 约
   24.1s/25.8s。先按 `assistant_response_start`、`tool_call_end`、首 PCM 拆分等待区间，确认是
   provider 排队、smart-turn endpointing，还是输出阶段慢。不要先删长尾点。
2. **Qwen 3.1 服务稳定性**：历史 total attempts 3720 才得到 2893 个最终 eligible；第三轮
   重跑 45 条才清零 invalid。应统计 connect/control/collector/playback 分类与时间分布。
3. **Step 低调用率**：82 个 eligible Fail 中 50 个没有工具调用，32 个主要为参数不一致。
   先比较 ASR 正确且 no-tool 的样本，避免把理解问题误归为 ASR。
4. **Step response 不收尾**：server VAD 与部分 manual tool response 有长音频/无 response.done
   历史。需要核对官方 response 生命周期和是否应主动 cancel；不能把 timeout 简单放宽到无限。
5. **ASR 因果**：`asr_failures.json` 只是候选。优先人工看实体/槽位被改写且最终参数同步错误的
   case；数字格式已用指定 norm.py 统一。
6. **候选工具拓扑**：当前每条只暴露期望工具，不能用本结果宣称 100 工具路由准确率。后续如
   测路由，必须独立编译 `--expose-all-tools` 或 oracle 无关的固定领域候选组。
7. **Step 覆盖扩展**：在 response 生命周期和 invalid 原因稳定前，不直接跑 3000 条。先重跑
   当前 17 invalid，并将 200 条的 no-tool/argument mismatch 分层审计。

## 8. 代码与 Git 状态

2026-09-24 已推送到 GitHub `main`：

- `ac1427d feat: add StepAudio realtime adapter`
- `b481072 feat: support Qwen Audio 3.1 realtime plus`
- `ada9610 feat: add provider audio resampling to cockpit campaigns`

当前根仓库 `.git` 在此受控环境挂载为只读，`HEAD` 仍显示旧的 `0df3c77`，工作树会把上述已
推送代码显示为未提交改动。不要 reset/checkout 或重建 `.git`。上次发布使用 `/tmp` writable
clone 完成。`reports/cockpit_four_comparison.py`、`reports/cockpit_five_comparison.py`、本 handoff
及这轮文档同步是后续本地改动，尚未发布。

运行工件、源测试集、冻结音频、报告和中间产物受 `.gitignore` 保护，不应提交。handoff 本身
是用户明确要求的项目交接文档，可以提交；不要把 API key、样本文本全集或整个报告复制进 Git。

## 9. 验证与安全边界

- API key 只通过 `DASHSCOPE_API_KEY`、`BYTEDANCE_LLM_API_KEY`、`STEPFUN_API_KEY` 注入。
- 不在源码、命令行参数、报告或 handoff 中记录 key 值。
- 默认自动化测试不能调用收费服务。
- `ruff` 和定向 Step/Qwen 单测已通过；全量 pytest 在此环境曾受 asyncio/线程退出阻塞影响，
  不得把只输出点号的卡住过程声称为通过。
- 原始 attempt 和历史评价不改写；invalid 重跑使用独立 run，并通过 shard report 选择首个
  eligible attempt。
- 冻结 TTS 资产不能删除，后续模型继续复用。
