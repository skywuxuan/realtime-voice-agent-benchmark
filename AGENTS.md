# 项目交接与开发约定

阶段事实以代码和自动化检查为准；设计目标不自动代表已实现。

## 当前范围

- Qwen 商业适配器支持 `qwen-audio-3.0-realtime-flash` 和
  `qwen-audio-3.0-realtime-plus`，使用服务端 smart-turn 与工具结果回注。
- Doubao 适配器实现 Seed Duplex 3.0 JSON Realtime 协议、音频流、自动判停、
  Function Calling 和工具结果回注。
- `benchmark.run --suite realtime` 支持 latency、interruption、backchannel、
  turn-taking、pause 和 overlap；不同 category 不合并 Overall Score。
- `benchmark.run --suite agent` 只执行 Adapter 实际产生的 `tool_call_end`。
  `expected_calls`、期望状态和禁止调用仅供离线 evaluator 使用。
- `dataset.cockpit` 把外部工具协议和 JSONL 数据编译为严格 JSON Schema、冻结音频和
  Agent suite。源标签与 schema 冲突时拒绝编译，不做静默类型转换。
- `scripts/cockpit_campaign.py` 支持按源行分片、封口后恢复、cache-only 音频复用和
  invalid-only retry。`reports/cockpit_comparison.py` 只生成本地对比报告。
- Step 保持 deferred，未核验的 live 协议不得伪造实现。

## 数据与发布边界

- 外部测试集、转换产物、编译 suite、冻结音频、运行工件、进度文件、中间产物和报告
  只保存在本地，并由 `.gitignore` 排除。
- 仓库文档不得记录实际测试内容、样本原文、数据规模、运行目录、报告路径、得分、
  pass/fail 数量或具体失败案例。
- 面向人的普通 JSON 使用稳定缩进和未转义中文；JSONL 保持一行一条。
- API key 只从进程环境读取，不写源码、配置、日志、报告或命令行实参。

## 工程边界

- Python 3.11 + `uv.lock`；厂商 wire protocol 只放在 `adapters/`，runner/evaluator
  不调用厂商 SDK。
- 新运行使用独立目录。原始事件、音频和历史评价不因修复而原地改写；离线重评使用
  独立 evaluation ID。
- 文本输入在 target session 之外渲染为内容寻址 WAV/metadata。正式比较前冻结音频，
  运行时使用 cache-only，避免意外调用收费 TTS。
- 异步磁盘测试在普通沙箱可能阻塞线程退出；遇到该环境问题应在受控执行环境重跑，
  不把收集数量当作通过数量。
- `.git` 在部分受控环境可能只读；不要擅自修复 Git 元数据或使用破坏性命令。

## 本地检查

```bash
uv sync --locked --python 3.11 --extra qwen
.venv/bin/ruff check .
.venv/bin/pytest -q
```

项目入口见 [README.md](README.md)，数据编译与候选工具拓扑见
[docs/cockpit-benchmark.md](docs/cockpit-benchmark.md)。
