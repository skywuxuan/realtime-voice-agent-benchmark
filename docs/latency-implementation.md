# Latency 实现

Latency 路径使用冻结 Scenario、音频哈希、显式语音边界、单调钟事件和虚拟播放，厂商事件解析只存在于 adapter。

## 执行路径

```text
Scenario + WAV
  -> simulator.input 按绝对 deadline 分帧发送
  -> RealtimeModelAdapter
  -> 接收音频与单调时间戳
  -> simulator.playback 虚拟消费
  -> EventRecorder 封口
  -> benchmark.evaluate 离线评价
```

`BufferedArtifacts` 在内存中预留 PCM offset，并通过有界 FIFO 写 raw event、音频和 normalized event。队列溢出、写入错误或引用不一致会令 case invalid，不会静默丢事件。

## 时间边界

输入在标注的 speech start/end 样本处拆帧。`user_audio_end` 是最后一个含语音输入块完成本地 transport 提交的时刻，不是文件末尾，也不声称是服务端接收时间。

`assistant_audio_start` 与首个有效 PCM chunk 共用接收时间。`assistant_playback_start/chunk/stop` 单独记录虚拟消费区间。`output_received.wav` 拼接收到的 PCM，`output.wav` 重建带间隙的播放时间线；二者都不是声卡测量。

## 有效性与统计

默认 profile 位于 `configs/latency.yaml`。发送或播放调度超过门槛记 invalid；有效模型超时保留在分母并标 censored，不填零延迟。负 TTFA 保留为 premature，不截成0。

指标包含 receive/playback TTFA 的 mean、P50/P90/P95/P99，以及可观测到的 VAD/response-start 分解。缺少证据的内部阶段保持 unavailable。自动边界、人工边界、turn mode、控制策略、模型配置和输入来源分组统计，不合成 Overall Score。

## 运行与重评

```bash
.venv/bin/python -m benchmark.run \
  --model qwen-realtime --suite realtime \
  --config configs/qwen-audio-3.0-realtime-flash-agent.yaml \
  --scenario scenarios/realtime/basic.yaml \
  --output runs/my-latency-run

.venv/bin/python -m benchmark.evaluate --run runs/my-latency-run
```

真实运行需要 adapter 对应的环境凭据；离线重评不需要 key。每次运行使用新目录，warmup、重复、失败和invalid全部保留，不择优覆盖。
