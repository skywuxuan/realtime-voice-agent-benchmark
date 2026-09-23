# Backchannel 实现

Backchannel 使用与 interruption 相同的双轮刺激执行器，并通过独立 evaluator 判断助手是否
错误中止原回答。本文只描述执行和评价语义，不记录实际语音内容或运行结果。

## 准备与运行

```bash
.venv/bin/python -m scripts.prepare_backchannel --transport stdlib

.venv/bin/python -m benchmark.run --model qwen-realtime --suite realtime \
  --scenario scenarios/realtime/backchannel_tts.yaml \
  --output runs/my-backchannel --warmups 0
.venv/bin/python -m benchmark.evaluate --run runs/my-backchannel
```

首次准备可能调用 TTS，凭据只从环境读取。冻结后的音频、metadata 和哈希供后续运行复用。

## 执行与指标

`backchannel_start/end` 对齐第二段语音的标注边界，不生成 `interrupt_start`，也不发送客户端
cancel。第一轮尾部静音可以在触发后停止，但两轮输入 PCM 不交错。

评价器复核完整刺激、缓冲前置条件、调度门槛、场景哈希和封口完整性：

- 目标 response 被服务端取消时，记录 false interruption。
- 同一 response 正常继续且证据完整时，记录 expected continuation。
- 缺少完成、播放被截断或出现无法归因的新 response 时，结果保持 unknown。
- 客户端取消污染、刺激不完整或调度超限时，case 为 invalid，不进入有效分母。

报告同时给出 known/unknown 数、证据覆盖率和保守上下界。不同边界来源、音色、控制策略和
调度 profile 必须分组，不能从单一语音配置外推总体能力。
